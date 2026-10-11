import os
import random
import torch
import networkx as nx

from algorithms.base import Router
from algorithms.deep_q.model import TrafficGNN
from common.networking.network import EdgeMap, ConnectionMap
from common.networking.line_graph import build_line_graph
from common.networking.dynamic_dist import DynamicDist

_FALLBACK_SPEED = 14.0  # only for zero-length/zero-time edges
_ROUTER_DEBUG_EVERY = int(os.getenv("DQ_ROUTER_DEBUG_EVERY", "200"))

_router_debug_count = 0

class DeepQRouter(Router):
    """
    Deep Q-routing — inference part. Has a copy of the deep Q model
    that is kept up-to-date via delta updates from Layer 4 (model_weights topic).

    Routing decision: chooses candidate with highest Q-value (reward = -travel_time).
    """

    def __init__(self, line_graph: nx.DiGraph, edge_map: EdgeMap, connection_map: ConnectionMap, hp: dict):
        self._line_graph = line_graph
        self._edge_map = edge_map
        self._connection_map = connection_map
        self._commit_hops = hp["commit_hops"]

        self._data, self._edge_to_idx = build_line_graph(edge_map=self._edge_map, connection_map=self._connection_map)

        # Normalise static features to full [0, 1] (min-max), matches DeepQLearner exactly
        _len_min, _len_max = self._data.x[:, 0].min().item(), self._data.x[:, 0].max().item()
        _time_min, _time_max = self._data.x[:, 1].min().item(), self._data.x[:, 1].max().item()
        self._data.x[:, 0] = (self._data.x[:, 0] - _len_min) / max(_len_max - _len_min, 1e-6)
        self._data.x[:, 1] = (self._data.x[:, 1] - _time_min) / max(_time_max - _time_min, 1e-6)

        N = len(self._edge_to_idx)
        self._ordered_edges: list[str] = [None] * N
        for edge_id, idx in self._edge_to_idx.items():
            self._ordered_edges[idx] = edge_id

        # Per-edge free-flow speed (length/time = lane speed limit)
        self._freeflow_list: list[float] = [
            (self._edge_map[e][2] / self._edge_map[e][3]) if self._edge_map[e][3] > 0 else _FALLBACK_SPEED
            for e in self._ordered_edges
        ]
        self._freeflow = torch.tensor(self._freeflow_list, dtype=torch.float32)

        self._dynamic = hp["dynamic_dist"]
        self.n_features = 6 if self._dynamic else 5
        self._model = TrafficGNN(in_channels=self.n_features)
        self._model.eval()

        self._traffic_state = {}
        self._plans: dict[str, list[str]] = {}  # vehicle -> remaining committed edges

        self._rev_line_graph = self._line_graph.reverse()

        self._dist_cache = {}
        self._dist_tensor_cache: dict[str, torch.Tensor] = {}

        _gmax = 0.0
        for e in self._edge_to_idx:
            try:
                lengths = nx.single_source_dijkstra_path_length(self._line_graph, e, weight="time")
                local_max = max(lengths.values(), default=0.0)
                if local_max > _gmax:
                    _gmax = local_max
            except Exception:
                pass
        self._global_max_dist: float = max(_gmax, 1.0)
        # Denominator for log-compressed dist feature, MUST match DeepQLearner
        self._log_dist_denom: float = float(torch.log1p(torch.tensor(self._global_max_dist)))
        self._dyn = (DynamicDist(self._ordered_edges, edge_map, connection_map, self._global_max_dist, hp["min_ratio"])
                     if self._dynamic else None)
        self._dyn_cache: dict[str, torch.Tensor] = {}  # per broadcast

        self.epsilon = 1.0

    def update_traffic_state(self, snapshot: dict[str, dict]):
        """Snapshot format: {edge_id: {"speed": float, "count": int}}"""
        self._traffic_state = snapshot
        self._dyn_cache.clear()

    def _get_dist_to_dest(self, dest_edge: str) -> dict[str, float]:
        if dest_edge not in self._dist_cache:
            try:
                dist = nx.single_source_dijkstra_path_length(self._rev_line_graph, dest_edge, weight="time")
                self._dist_cache[dest_edge] = dict(dist)
            except nx.NetworkXNoPath:
                self._dist_cache[dest_edge] = {}
        return self._dist_cache[dest_edge]

    def _get_dist_tensor(self, dest_edge: str) -> torch.Tensor:
        """Return cached [N] distance tensor normalised by global network diameter"""
        if dest_edge not in self._dist_tensor_cache:
            dist = self._get_dist_to_dest(dest_edge)
            raw = torch.tensor(
                [dist.get(e, self._global_max_dist) for e in self._ordered_edges],
                dtype=torch.float32,
            )
            # Log-compressed, sharper contrast near the goal. MUST match DeepQLearner.
            d = torch.log1p(raw) / self._log_dist_denom
            self._dist_tensor_cache[dest_edge] = d
        return self._dist_tensor_cache[dest_edge]

    def _raw_speeds(self) -> list[float]:
        _E: dict = {}
        state = self._traffic_state
        return [state.get(e, _E).get("speed", ff) for e, ff in zip(self._ordered_edges, self._freeflow_list)]

    def _get_dyn_tensor(self, dest_edge: str, raw_speeds: list[float]) -> torch.Tensor:
        if dest_edge not in self._dyn_cache:
            self._dyn_cache[dest_edge] = torch.from_numpy(self._dyn.feature(raw_speeds, dest_edge))
        return self._dyn_cache[dest_edge]

    def _build_feature_tensor(self, dest_edge: str) -> torch.Tensor:
        """Make [N, 5] (or [N, 6] with the dynamic distance) feature matrix"""
        _E: dict = {}
        state = self._traffic_state
        raw_speeds = self._raw_speeds()
        # Congestion ratio: current speed / per-edge free-flow, clamped to 1.0; missing edge → 1.0
        speeds = torch.tensor(raw_speeds, dtype=torch.float32).div_(self._freeflow).clamp_(max=1.0)
        counts = torch.tensor(
            [state.get(e, _E).get("count", 0.0) / 30.0 for e in self._ordered_edges],
            dtype=torch.float32,
        )
        cols = [speeds, counts, self._get_dist_tensor(dest_edge)]
        if self._dynamic:
            cols.append(self._get_dyn_tensor(dest_edge, raw_speeds))
        return torch.cat([self._data.x, torch.stack(cols, dim=1)], dim=1)

    def find_next_edge(self, current_edge: str, dest_edge: str, vehicle_id: str) -> str | None:
        """Return next edge toward dest_edge using Deep Q-values or random (based on epsilon)"""
        candidates = self._connection_map.get(current_edge, [])
        if not candidates:
            return None

        plan = self._plans.pop(vehicle_id, None)
        if random.random() < self.epsilon:
            return random.choice(candidates)
        if plan and plan[0] in candidates:
            if len(plan) > 1:
                self._plans[vehicle_id] = plan[1:]
            return plan[0]

        x = self._build_feature_tensor(dest_edge)
        with torch.no_grad():
            q_values = self._model(x, self._data.edge_index)

        q_by_cand = {e: q_values[self._edge_to_idx[e]].item() for e in candidates}
        chosen = max(candidates, key=lambda e: q_by_cand[e])
        if self._commit_hops > 1 and chosen != dest_edge:
            self._plans[vehicle_id] = self._greedy_plan(q_values, current_edge, chosen, dest_edge, self._commit_hops - 1)

        global _router_debug_count
        _router_debug_count += 1
        if _router_debug_count % _ROUTER_DEBUG_EVERY == 0:
            dist = self._get_dist_to_dest(dest_edge)
            dyn = self._dyn.seconds(self._raw_speeds(), dest_edge) if self._dynamic else None
            rows = ", ".join(
                f"{e}(Q={q_by_cand[e]:+.3f} d={dist.get(e, self._global_max_dist):.0f}s"
                f"{f' dd={dyn[self._edge_to_idx[e]]:.0f}s' if dyn is not None else ''})"
                f"{'*' if e == chosen else ''}"
                for e in candidates
            )
            closest = min(candidates, key=lambda e: dist.get(e, self._global_max_dist))
            d_chosen = dist.get(chosen, self._global_max_dist)
            d_closest = dist.get(closest, self._global_max_dist)
            # Only a real mismatch if the chosen edge is meaningfully farther (ignore ~ties)
            follows = "OK" if d_chosen - d_closest <= 3.0 else f"MISMATCH(+{d_chosen - d_closest:.0f}s)"
            print(f"[router] {current_edge} -> dest {dest_edge} | {rows} | {follows}")

        return chosen

    def _greedy_plan(self, q_values, current_edge: str, start: str, dest_edge: str, hops: int) -> list[str]:
        """Follow the argmax next edge from start for up to hops edges"""
        plan, seen, cur = [], {current_edge, start}, start
        for _ in range(hops):
            succ = self._connection_map.get(cur, [])
            if cur == dest_edge or not succ:
                break
            nxt = max(succ, key=lambda e: q_values[self._edge_to_idx[e]].item())
            if nxt in seen:
                break
            plan.append(nxt)
            seen.add(nxt)
            cur = nxt
        return plan

    def set_epsilon(self, value: float) -> None:
        self.epsilon = value

    def clear_vehicle(self, vehicle_id: str) -> None:
        self._plans.pop(vehicle_id, None)

    def apply_update(self, msg: dict) -> bool:
        """Load broadcast weights and epsilon; a message from a learner with another feature count is dropped"""
        n_features = msg.get("n_features", 5)
        if n_features != self.n_features:
            print(f"[DeepQRouter] dropped model_weights with n_features={n_features}, "
                  f"this router uses {self.n_features} (check dynamic_dist)")
            return False
        state_dict = {k: torch.tensor(v) for k, v in msg["delta"].items()}
        self._model.load_state_dict(state_dict, strict=False)
        self._model.eval()
        if "epsilon" in msg:
            self.set_epsilon(msg["epsilon"])
        return True
