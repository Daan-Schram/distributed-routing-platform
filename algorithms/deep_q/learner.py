import collections
import json
import os
import random
import time

import networkx as nx
import numpy as np
import torch
import torch.nn.functional as F
from kafka import KafkaConsumer, KafkaProducer

import algorithms
from algorithms.deep_q.model import TrafficGNN
from common.config import KAFKA_BOOTSTRAP, NETWORK_FILE, TOPIC_EXPERIENCE_REPLAYS, TOPIC_MODEL_WEIGHTS, TOPIC_METRICS, RUN_ID_FILE, LEARNER_GROUP_ID
from common.learner_bus import publish_metrics, publish_pressure
from common.networking.network import load_network
from common.networking.line_graph import build_line_graph
from common.networking.dynamic_dist import DynamicDist
from common.monitoring.resource_monitor import ResourceMonitor
from pathlib import Path

PROBE_DESTS = int(os.getenv("DQ_PROBE_DESTS", "8"))
SAVE_MODEL = os.getenv("SAVE_MODEL", "true").lower() == "true"
DEBUG_FEATURES_EVERY = int(os.getenv("DQ_DEBUG_FEATURES_EVERY", "200"))
_FEATURE_NAMES = ["len", "time", "speed", "count", "dist", "dyn"]
_DYN_CACHE_SIZE = 4096

class ReplayBuffer:
    def __init__(self, maxlen: int):
        self._buffer = collections.deque(maxlen=maxlen)

    def push(self, experience: dict):
        self._buffer.append(experience)

    def sample(self, batch_size: int) -> list[dict]:
        return random.sample(self._buffer, batch_size)

    def __len__(self) -> int:
        return len(self._buffer)


class DeepQLearner:
    def __init__(self, producer: KafkaProducer, hp: dict):
        self._producer = producer
        self.hp = hp
        self._dynamic = hp["dynamic_dist"]
        self.n_features = 6 if self._dynamic else 5

        graph, line_graph, edge_map, connection_map = load_network(NETWORK_FILE)
        self._graph = graph
        self._line_graph = line_graph
        self._edge_map = edge_map
        self._connection_map = connection_map

        self._data, self._edge_to_idx = build_line_graph(edge_map, connection_map)

        # Normalise static features to full [0, 1] (min-max, not divide-by-max)
        _len_min, _len_max = self._data.x[:, 0].min().item(), self._data.x[:, 0].max().item()
        _time_min, _time_max = self._data.x[:, 1].min().item(), self._data.x[:, 1].max().item()
        self._data.x[:, 0] = (self._data.x[:, 0] - _len_min) / max(_len_max - _len_min, 1e-6)
        self._data.x[:, 1] = (self._data.x[:, 1] - _time_min) / max(_time_max - _time_min, 1e-6)

        N = len(self._edge_to_idx)
        self._ordered_edges: list[str] = [None] * N
        for edge_id, idx in self._edge_to_idx.items():
            self._ordered_edges[idx] = edge_id

        # Successor indices per edge, for the max-bootstrap target
        self._succ_idx: dict[str, torch.Tensor] = {}
        for e, succs in connection_map.items():
            idxs = [self._edge_to_idx[c] for c in succs if c in self._edge_to_idx]
            if idxs:
                self._succ_idx[e] = torch.tensor(idxs, dtype=torch.long)

        self._rev_line_graph = line_graph.reverse()
        self._dist_cache: dict[str, dict[str, float]] = {}
        self._dist_tensor_cache: dict[str, torch.Tensor] = {}

        _gmax = 0.0
        for e in self._edge_to_idx:
            try:
                lengths = nx.single_source_dijkstra_path_length(line_graph, e, weight="time")
                local_max = max(lengths.values(), default=0.0)
                if local_max > _gmax:
                    _gmax = local_max
            except Exception:
                pass
        self._global_max_dist: float = max(_gmax, 1.0)
        # Denominator for log-compressed dist feature, sharper near goal
        self._log_dist_denom: float = float(torch.log1p(torch.tensor(self._global_max_dist)))
        print(f"[DeepQLearner] Global max dist: {self._global_max_dist:.1f}s")
        self._dyn = (DynamicDist(self._ordered_edges, edge_map, connection_map, self._global_max_dist, hp["min_ratio"])
                     if self._dynamic else None)
        self._dyn_lru: collections.OrderedDict = collections.OrderedDict()  # (speeds bytes, dest) -> [N]
        print(f"[DeepQLearner] n_features={self.n_features} (dynamic_dist={self._dynamic})")

        # Reward scale: mean free-flow edge travel time from this network
        # Dividing travel_time by this keeps rewards near [-1, 0] per edge, to prevent Q value divergence
        self._reward_scale = sum(v[3] for v in edge_map.values()) / len(edge_map)
        print(f"[DeepQLearner] Reward scale: {self._reward_scale:.2f}s (mean free-flow edge time)")

        self._device = torch.device(hp["device"])
        print(f"[DeepQLearner] Using device: {self._device}")

        self._data = self._data.to(self._device)
        self._succ_idx = {k: v.to(self._device) for k, v in self._succ_idx.items()}

        # Per-edge free-flow speed (length/time = lane speed limit)
        self._freeflow = torch.tensor(
            [(edge_map[e][2] / edge_map[e][3]) if edge_map[e][3] > 0 else 14.0 for e in self._ordered_edges],
            dtype=torch.float32, device=self._device,
        )

        self._online_net = TrafficGNN(in_channels=self.n_features).to(self._device)
        self._target_net = TrafficGNN(in_channels=self.n_features).to(self._device)
        self._target_net.load_state_dict(self._online_net.state_dict())
        self._target_net.eval()

        self._optimizer = torch.optim.Adam(self._online_net.parameters(), lr=hp["learning_rate"])

        self._replay_buffer = ReplayBuffer(hp["max_buffer_size"])
        self._recent_buffer: collections.deque = collections.deque(maxlen=hp["recent_buffer_size"])
        # Sliding window of recent incoming experiences' revisit flags/counts — reflects the
        # CURRENT policy's looping behaviour (the live version of looped_pct). Small window
        # so it responds quickly to loops rising/falling during a run.
        self._revisit_window: collections.deque = collections.deque(maxlen=500)
        self._probe_dests: list[str] = []
        self._train_steps = 0
        self._experiences_seen = 0
        self._epsilon = hp["epsilon_start"]

        run_id_file = Path(RUN_ID_FILE)
        self._run_id = run_id_file.read_text().strip() if run_id_file.exists() else "unknown"

    def _get_dist_to_dest(self, dest_edge: str) -> dict[str, float]:
        """Returns distance to destination edge from cache or compute"""
        if dest_edge not in self._dist_cache:
            try:
                dist = nx.single_source_dijkstra_path_length(self._rev_line_graph, dest_edge, weight="time")
                self._dist_cache[dest_edge] = dict(dist)
            except nx.NetworkXNoPath:
                self._dist_cache[dest_edge] = {}
        return self._dist_cache[dest_edge]

    def _get_dist_tensor(self, dest_edge: str) -> torch.Tensor:
        """Return cached [N] distance tensor for dest_edge, normalised by global network diameter"""
        if dest_edge not in self._dist_tensor_cache:
            dist = self._get_dist_to_dest(dest_edge)
            raw = torch.tensor(
                [dist.get(e, self._global_max_dist) for e in self._ordered_edges],
                dtype=torch.float32, device=self._device,
            )
            # Log-compressed: much sharper contrast near the goal, gentle far away. [0, diameter] -> [0, 1]
            d = torch.log1p(raw) / self._log_dist_denom
            self._dist_tensor_cache[dest_edge] = d
        return self._dist_tensor_cache[dest_edge]

    def _get_dyn_tensor(self, speeds, dest_edge: str) -> torch.Tensor:
        """Dynamic distance column for one snapshot, LRU-cached on the exact speeds"""
        s = np.asarray(speeds, dtype=np.float32)
        key = (s.tobytes(), dest_edge)
        t = self._dyn_lru.get(key)
        if t is None:
            t = torch.from_numpy(self._dyn.feature(s, dest_edge)).to(self._device)
            self._dyn_lru[key] = t
            if len(self._dyn_lru) > _DYN_CACHE_SIZE:
                self._dyn_lru.popitem(last=False)
        else:
            self._dyn_lru.move_to_end(key)
        return t

    def push(self, experience: dict):
        self._replay_buffer.push(experience)
        self._recent_buffer.append(experience)
        self._revisit_window.append(
            (1.0 if experience.get("is_revisit") else 0.0, experience.get("revisit_count", 0))
        )
        self._experiences_seen += 1
        progress = min(1.0, self._experiences_seen / self.hp["epsilon_anneal_steps"])
        self._epsilon = max(self.hp["epsilon_min"], self.hp["epsilon_start"] - (self.hp["epsilon_start"] - self.hp["epsilon_min"]) * progress)

    def revisit_stats(self) -> tuple[float, float]:
        """(fraction of recent experiences that were revisits, mean revisit_count)"""
        w = self._revisit_window
        if not w:
            return 0.0, 0.0
        return sum(f for f, _ in w) / len(w), sum(c for _, c in w) / len(w)

    def _spearman(self, a, b) -> float:
        if len(a) < 3:
            return 0.0
        ra = np.argsort(np.argsort(a)).astype(float)
        rb = np.argsort(np.argsort(b)).astype(float)
        ra -= ra.mean()
        rb -= rb.mean()
        denom = float(np.sqrt((ra ** 2).sum() * (rb ** 2).sum()))
        return float((ra * rb).sum() / denom) if denom else 0.0

    def _probe_features(self, dest_edge: str, snapshot: dict) -> torch.Tensor:
        speeds = torch.tensor(snapshot["speeds"], dtype=torch.float32, device=self._device)
        counts = torch.tensor(snapshot["counts"], dtype=torch.float32, device=self._device)
        cols = [
            self._data.x[:, 0],
            self._data.x[:, 1],
            (speeds / self._freeflow).clamp(max=1.0),
            counts / 30.0,
            self._get_dist_tensor(dest_edge),
        ]
        if self._dynamic:
            cols.append(self._get_dyn_tensor(snapshot["speeds"], dest_edge))
        return torch.stack(cols, dim=1)

    def value_probe(self) -> dict | None:
        """Read Q at every edge for some fixed destinations and score the value function"""
        if not self._recent_buffer:
            return None
        snapshot = self._recent_buffer[-1].get("state_after")
        if not snapshot or "speeds" not in snapshot:
            return None

        if not self._probe_dests:
            dests = collections.Counter(
                e["dest_edge"] for e in self._recent_buffer
                if e.get("dest_edge") in self._edge_to_idx
            )
            self._probe_dests = [d for d, _ in dests.most_common(PROBE_DESTS)]
        if not self._probe_dests:
            return None

        corrs, dyn_corrs, q_spans, follows, total = [], [], [], 0, 0
        self._online_net.eval()
        try:
            for dest in self._probe_dests:
                dist = self._get_dist_to_dest(dest)
                with torch.no_grad():
                    q = self._online_net(self._probe_features(dest, snapshot),
                                         self._data.edge_index).squeeze(-1)

                idx, opt = [], []
                for e, i in self._edge_to_idx.items():
                    if e in dist and e != dest:
                        idx.append(i)
                        opt.append(-(self._edge_map[e][3] + dist[e]))
                if len(idx) >= 3:
                    qv = q[idx].cpu().numpy()
                    corrs.append(self._spearman(qv, opt))
                    q_spans.append(float(qv.max() - qv.min()))
                    if self._dynamic:
                        sec = self._dyn.seconds(snapshot["speeds"], dest)
                        et = self._dyn.edge_times(snapshot["speeds"])
                        dyn_corrs.append(self._spearman(qv, [-(et[i] + sec[i]) for i in idx]))

                big = float("inf")
                for e in self._ordered_edges:
                    cands = [c for c in self._connection_map.get(e, []) if c in self._edge_to_idx]
                    if len(cands) < 2 or e == dest:
                        continue
                    chosen = max(cands, key=lambda c: q[self._edge_to_idx[c]].item())
                    d_best = min(dist.get(c, big) for c in cands)
                    total += 1
                    if dist.get(chosen, big) - d_best <= 3.0:
                        follows += 1
        finally:
            self._online_net.train()

        out = {
            "probe_q_vs_optimal": round(sum(corrs) / len(corrs), 4) if corrs else 0.0,
            "probe_q_span": round(sum(q_spans) / len(q_spans), 4) if q_spans else 0.0,
            "probe_follows_pct": round(100.0 * follows / total, 2) if total else 0.0,
            "probe_dests": len(self._probe_dests),
        }
        if self._dynamic:
            out["probe_q_vs_dynamic"] = round(sum(dyn_corrs) / len(dyn_corrs), 4) if dyn_corrs else 0.0
        return out

    def train_step(self) -> tuple[float, float, float]:
        """Sample a mini-batch and apply one gradient update. Returns (loss, grad_norm, |Q|).

        recent_only=true:  all batch_size samples from recent_buffer
        recent_only=false: batch_size/2 from recent_buffer + batch_size/2 from replay_buffer
        """
        _skipped = (0.0, 0.0, 0.0)
        if self.hp["recent_only"]:
            if len(self._recent_buffer) < self.hp["batch_size"]:
                return _skipped
            raw = random.sample(list(self._recent_buffer), self.hp["batch_size"])
        else:
            if len(self._replay_buffer) < self.hp["min_buffer_size"] or len(self._recent_buffer) < self.hp["batch_size"] // 2:
                return _skipped
            recent_raw = random.sample(list(self._recent_buffer), self.hp["batch_size"] // 2)
            replay_raw = self._replay_buffer.sample(self.hp["batch_size"] // 2)
            raw = recent_raw + replay_raw

        batch = [exp for exp in raw if exp.get("prev_edge") in self._edge_to_idx]
        if not batch:
            return _skipped

        B = len(batch)
        N = len(self._edge_to_idx)

        # Build speed/count arrays for the full batch in one numpy pass
        speeds_cur = np.empty((B, N), dtype=np.float32)
        counts_cur = np.empty((B, N), dtype=np.float32)
        speeds_nxt = np.empty((B, N), dtype=np.float32)
        counts_nxt = np.empty((B, N), dtype=np.float32)
        dist_cols = torch.empty(B, N, device=self._device)
        F_ = self.n_features
        if self._dynamic:
            dyn_cur = torch.empty(B, N, device=self._device)
            dyn_nxt = torch.empty(B, N, device=self._device)

        for i, exp in enumerate(batch):
            sc = exp["state_before"]
            sn = exp["state_after"]
            speeds_cur[i] = sc["speeds"]
            counts_cur[i] = sc["counts"]
            speeds_nxt[i] = sn["speeds"]
            counts_nxt[i] = sn["counts"]
            dist_cols[i] = self._get_dist_tensor(exp["dest_edge"])
            if self._dynamic:
                dyn_cur[i] = self._get_dyn_tensor(speeds_cur[i], exp["dest_edge"])
                dyn_nxt[i] = self._get_dyn_tensor(speeds_nxt[i], exp["dest_edge"])

        # Speed → per-edge congestion ratio (clamped to 1.0); count by fixed scale. Matches router.
        sc_t = (torch.from_numpy(speeds_cur).to(self._device) / self._freeflow).clamp_(max=1.0)
        cc_t = torch.from_numpy(counts_cur).to(self._device) / 30.0
        sn_t = (torch.from_numpy(speeds_nxt).to(self._device) / self._freeflow).clamp_(max=1.0)
        cn_t = torch.from_numpy(counts_nxt).to(self._device) / 30.0

        static = self._data.x.unsqueeze(0).expand(B, -1, -1)  # [B, N, 2]
        cur_cols = [static[:, :, 0], static[:, :, 1], sc_t, cc_t, dist_cols]
        nxt_cols = [static[:, :, 0], static[:, :, 1], sn_t, cn_t, dist_cols]
        if self._dynamic:
            cur_cols.append(dyn_cur)
            nxt_cols.append(dyn_nxt)
        x_cur = torch.stack(cur_cols, dim=2)  # [B, N, F]
        x_nxt = torch.stack(nxt_cols, dim=2)  # [B, N, F]

        if self._train_steps % DEBUG_FEATURES_EVERY == 0:
            fc = x_cur.reshape(-1, F_)
            cols = " ".join(
                f"{_FEATURE_NAMES[i]}[{fc[:, i].min():.2f},{fc[:, i].max():.2f},avg{fc[:, i].mean():.2f}]"
                for i in range(F_)
            )
            print(f"[feat] step={self._train_steps} B={B} {cols}  nan={torch.isnan(fc).any().item()}")

        # Batched edge_index, B disconnected copies of the graph
        offsets = torch.arange(B, device=self._device).unsqueeze(1) * N # [B, 1]
        ei_src = self._data.edge_index[0].unsqueeze(0).expand(B, -1) + offsets # [B, E]
        ei_dst = self._data.edge_index[1].unsqueeze(0).expand(B, -1) + offsets
        batched_ei = torch.stack([ei_src.reshape(-1), ei_dst.reshape(-1)]) # [2, B*E]

        # online for q_taken, on the next state, online picks the successor and target
        # scores it (Double DQN)
        q_cur = self._online_net(x_cur.reshape(B * N, F_), batched_ei) # [B*N, 1]

        with torch.no_grad():
            x_nxt_flat = x_nxt.reshape(B * N, F_)
            q_nxt_target = self._target_net(x_nxt_flat, batched_ei)
            q_nxt_online = self._online_net(x_nxt_flat, batched_ei)

        losses = []
        for i, exp in enumerate(batch):
            offset = i * N
            # travel_time and q_taken both refer to prev_edge (the edge being left), bootstrap from the entered edge
            q_taken = q_cur[offset + self._edge_to_idx[exp["prev_edge"]]]

            # Bootstrap from the best reachable successor
            terminal = exp.get("new_edge") == exp.get("dest_edge")
            succ = self._succ_idx.get(exp["prev_edge"])
            if terminal or succ is None:
                q_next = torch.tensor([0.0], device=self._device)
            else:
                rows = offset + succ
                best = rows[q_nxt_online[rows].argmax()]
                q_next = q_nxt_target[best]

            # Cap extreme travel times
            travel_time = min(exp["travel_time"], self._reward_scale * self.hp["jam_cap_multiplier"])
            dist = self._get_dist_to_dest(exp["dest_edge"])
            d_before = dist.get(exp["prev_edge"], self._global_max_dist)
            d_after = dist.get(exp["new_edge"],  self._global_max_dist)
            progress = (d_before - d_after) / self._reward_scale
            r = -(travel_time / self._reward_scale) + self.hp["progress_weight"] * progress
            r = max(-10.0, r)
            # Loop penalty, applied after the -10 clip so congested loop edges still feel it
            if not terminal and exp.get("is_revisit"):
                r -= self.hp["loop_penalty"]
            if terminal:
                y = torch.tensor([r + self.hp["arrival_bonus"]], device=self._device)
            else:
                y = r + self.hp["gamma"] * q_next
            losses.append(F.huber_loss(q_taken, y.detach(), delta=1.0, reduction="none"))

        loss = torch.stack(losses).mean()
        self._optimizer.zero_grad()
        loss.backward()
        grad_norm = torch.nn.utils.clip_grad_norm_(self._online_net.parameters(), self.hp["grad_clip_norm"])
        self._optimizer.step()

        self._train_steps += 1

        q_mean = q_cur.abs().mean().item()
        return loss.item(), grad_norm.item(), q_mean

    def save_checkpoint(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        torch.save({
            "state_dict": self._online_net.state_dict(),
            "optimizer": self._optimizer.state_dict(),
            "epsilon": self._epsilon,
            "train_steps": self._train_steps,
            "experiences_seen": self._experiences_seen,
            "n_features": self.n_features,
        }, tmp)
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    print(f"[DeepQLearner] Checkpoint not saved, {path.name} is in use; retrying at the next broadcast")
                    return
                time.sleep(0.05)

    def load_checkpoint(self, path) -> None:
        """Validate in a fresh net first, so a failed load leaves both nets untouched"""
        ckpt = torch.load(path, map_location=self._device)
        n = ckpt.get("n_features", 5)
        if n != self.n_features:
            raise ValueError(f"checkpoint has n_features={n}, this learner uses {self.n_features} "
                             f"(dynamic_dist={self._dynamic}); it will be overwritten")
        fresh = TrafficGNN(in_channels=self.n_features).to(self._device)
        fresh.load_state_dict(ckpt["state_dict"], strict=False)
        self._online_net.load_state_dict(fresh.state_dict())
        self._target_net.load_state_dict(fresh.state_dict())
        self._target_net.eval()
        if ckpt.get("optimizer"):
            try:
                self._optimizer.load_state_dict(ckpt["optimizer"])
            except (ValueError, KeyError) as e:
                print(f"[DeepQLearner] Optimizer state not restored: {e}")
        self._epsilon = ckpt.get("epsilon", self.hp["epsilon_min"])
        self._train_steps = ckpt.get("train_steps", 0)
        self._experiences_seen = ckpt.get("experiences_seen", 0)
        if self.hp["epsilon_reset_on_load"]:
            self._experiences_seen = 0
            self._epsilon = self.hp["epsilon_start"]
        print(f"[DeepQLearner] Loaded checkpoint: train_steps={self._train_steps} eps={self._epsilon:.3f} "
              f"exps_seen={self._experiences_seen} eps_reset={self.hp['epsilon_reset_on_load']}")

    def _maybe_update_target(self):
        if self._train_steps % self.hp["target_update_every"] == 0:
            self._target_net.load_state_dict(self._online_net.state_dict())
            print(f"[DeepQLearner] Target network updated at step {self._train_steps}")

    def _broadcast_weights(self):
        weights = {k: v.tolist() for k, v in self._online_net.state_dict().items()}
        self._producer.send(TOPIC_MODEL_WEIGHTS, {
            "algorithm": "deep_q",
            "delta": weights,
            "epsilon": self._epsilon,
            "n_features": self.n_features,
        })
        self._producer.flush()

        self._producer.send(TOPIC_METRICS, {
            "type": "deep_q_broadcast",
            "train_steps": self._train_steps,
            "run_id": self._run_id,
        })

def run(hp: dict) -> None:
    """Main loop, consume experience_replays, fill replay buffer, train, broadcast weights"""
    import time

    monitor = ResourceMonitor(layer="layer4", process="deep_q_learner")
    monitor.start()

    producer = KafkaProducer(
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
    checkpoint_path = Path(
        os.getenv("CHECKPOINT_PATH", algorithms.checkpoint_path("deep_q"))
    )
    load_ckpt = os.getenv("LOAD_CHECKPOINT", "false").lower() == "true"

    learner = DeepQLearner(producer, hp)
    loaded_checkpoint = False
    if load_ckpt:
        if checkpoint_path.exists():
            try:
                learner.load_checkpoint(checkpoint_path)
                loaded_checkpoint = True
            except Exception as e:
                print(f"[DeepQLearner] WARNING: checkpoint unreadable ({e}), starting from random weights")
        else:
            print(f"[DeepQLearner] WARNING: LOAD_CHECKPOINT set but {checkpoint_path} does not exist "
                  f", starting from random weights (a warm start did not happen)")
    print(f"[DeepQLearner] Ready, online net {sum(p.numel() for p in learner._online_net.parameters())} params")
    print(f"[DeepQLearner] load_checkpoint={load_ckpt}  save_model={SAVE_MODEL}  checkpoint={checkpoint_path}")
    start_state = {"loaded_checkpoint": loaded_checkpoint, "train_steps_at_start": learner._train_steps,
                   "learner_device": str(learner._device)}

    if SAVE_MODEL:
        import signal

        def _save_and_exit(_sig, _frm):
            try:
                learner.save_checkpoint(checkpoint_path)
                print("[DeepQLearner] Checkpoint saved on shutdown")
            finally:
                raise SystemExit(0)

        signal.signal(signal.SIGTERM, _save_and_exit)

    consumer = KafkaConsumer(
        TOPIC_EXPERIENCE_REPLAYS,
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        auto_offset_reset="latest",
        group_id=LEARNER_GROUP_ID,
    )
    print("[DeepQLearner] Consuming experience_replays...")
    print("[Learner] Ready")

    recent_losses = collections.deque(maxlen=hp["broadcast_every"])
    recent_grad_norms = collections.deque(maxlen=hp["broadcast_every"])
    recent_q_mags = collections.deque(maxlen=hp["broadcast_every"])
    t_last_broadcast = time.monotonic()

    try:
        while True:
            # Drain up to 500 records available in Kafka for training
            raw = consumer.poll(timeout_ms=1000, max_records=500)
            if not raw:
                continue

            batch = [msg for msgs in raw.values() for msg in msgs]
            n = len(batch)

            for msg in batch:
                learner.push(msg.value)

            warmup_ok = (len(learner._recent_buffer) >= hp["batch_size"]) if hp["recent_only"] \
                else (len(learner._replay_buffer) >= hp["min_buffer_size"] and len(learner._recent_buffer) >= hp["batch_size"] // 2)
            if not warmup_ok:
                buf = len(learner._replay_buffer)
                rec = len(learner._recent_buffer)
                if buf % 100 < n:
                    mode_tag = "recent-only" if hp["recent_only"] else "replay+recent"
                    print(f"[DeepQLearner] warming up [{mode_tag}]  replay={buf}/{hp['min_buffer_size']}  "
                          f"recent={rec}/{hp['batch_size']}")
                publish_pressure(producer, learner._run_id, n, hp["train_steps_per_cycle"], start_state)
                continue

            for _ in range(hp["train_steps_per_cycle"]):
                loss, grad_norm, q_mag = learner.train_step()
                recent_losses.append(loss)
                recent_grad_norms.append(grad_norm)
                recent_q_mags.append(q_mag)
                learner._maybe_update_target()

                if learner._train_steps % hp["broadcast_every"] == 0:
                    learner._broadcast_weights()
                    if SAVE_MODEL:
                        learner.save_checkpoint(checkpoint_path)
                    publish_pressure(producer, learner._run_id, n, hp["train_steps_per_cycle"], start_state)

                    elapsed = time.monotonic() - t_last_broadcast
                    steps_per_sec = hp["broadcast_every"] / elapsed if elapsed > 0 else 0
                    t_last_broadcast = time.monotonic()

                    avg_loss = sum(recent_losses) / len(recent_losses)
                    avg_grad = sum(recent_grad_norms) / len(recent_grad_norms)
                    avg_q = sum(recent_q_mags) / len(recent_q_mags)
                    revisit_rate, revisit_mean = learner.revisit_stats()
                    probe = learner.value_probe() or {}
                    behind = "BEHIND" if n > hp["train_steps_per_cycle"] else "OK"
                    mode_tag = "recent-only" if hp["recent_only"] else "replay+recent"
                    print(
                        f"[DeepQLearner] step={learner._train_steps:>4}  "
                        f"loss={avg_loss:.4f}  "
                        f"|Q|={avg_q:.2f}  "
                        f"grad={avg_grad:.2f}  "
                        f"eps={learner._epsilon:.3f}  "
                        f"revisit={revisit_rate:.0%}  "
                        + (f"span={probe['probe_q_span']:.2f}  corr={probe['probe_q_vs_optimal']:+.2f}  "
                           if probe else "") +
                        f"exps seen={learner._experiences_seen}/{hp['epsilon_anneal_steps']}  "
                        f"buf={len(learner._replay_buffer)}  "
                        f"recent={len(learner._recent_buffer)}  "
                        f"steps/s={steps_per_sec:.1f}  "
                        f"poll={n} [{behind}] [{mode_tag}]"
                    )
                    publish_metrics(producer, learner._run_id, "deep_q", learner._train_steps, {
                        "loss": round(avg_loss, 6),
                        "grad_norm": round(avg_grad, 4),
                        "q_mean": round(avg_q, 4),
                        "epsilon": round(learner._epsilon, 4),
                        "revisit_rate": round(revisit_rate, 4),
                        "revisit_mean": round(revisit_mean, 4),
                        **probe,
                    }, start_state)

    except KeyboardInterrupt:
        pass
    finally:
        consumer.close()
        producer.close()
