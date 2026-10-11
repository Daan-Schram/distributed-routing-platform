"""Trains the real DeepQLearner against the graph-only env

Learner and router are used unmodified.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import algorithms

_DQ = algorithms.get("deep_q")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="T1/T2 graph-only Deep-Q training harness")
    p.add_argument("--network", default="irregular_6x6_very_short_and_calm")
    p.add_argument("--policy", default="deep_q", choices=["deep_q", "dijkstra", "dynamic_dijkstra", "random"])
    p.add_argument("--episodes", type=int, default=40)
    p.add_argument("--route-file", action="store_true",
                   help="take the trips (OD, depart times) and the jam edges from the network's .rou.xml, "
                        "as production does; ignores --vehicles and --depart-period")
    p.add_argument("--warm-start-reset", action="store_true",
                   help="between episodes act like production's warm start (a new learner loading the "
                        "checkpoint): empty the replay buffers and copy the online net to the target net")
    p.add_argument("--vehicles", type=int, default=100)
    p.add_argument("--depart-period", type=float, default=5.0)
    p.add_argument("--max-hops", type=int, default=150)
    p.add_argument("--congestion-k", type=float, default=0.0)
    p.add_argument("--min-path-hops", type=int, default=3)
    p.add_argument("--blind-traffic", action="store_true",
                   help="congestion still drives travel time, but features report free-flow")
    p.add_argument("--jam-top-edges", type=int, default=0,
                   help="slow the N edges most used by free-flow shortest paths, creating a "
                        "bottleneck static Dijkstra cannot see")
    p.add_argument("--jam-factor", type=float, default=5.0,
                   help="divide speed by this on jammed edges")
    p.add_argument("--speed-ema", type=float, default=None,
                   help="EMA weight on observed edge speed at broadcast time; 1.0 = instantaneous. "
                        "Defaults to config.SPEED_EMA_ALPHA so the harness matches production")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--eval-every", type=int, default=2)
    p.add_argument("--eval-vehicles", type=int, default=50)
    p.add_argument("--eval-seed", type=int, default=12345)
    p.add_argument("--eval-epsilons", default="0",
                   help="comma-separated epsilons to evaluate the same model at; "
                        ">0 desynchronises otherwise-identical vehicles")
    p.add_argument("--poll-size", type=int, default=28,
                   help="transitions per training cycle; the learner does TRAIN_STEPS_PER_CYCLE "
                        "steps per poll regardless of batch size, and a poll typically returns "
                        "~28 records, not the 500 max_records cap")
    p.add_argument("--eval-seconds", type=float, default=60,
                   help="wall-clock budget per evaluation episode; raise it when sweeping "
                        "many --eval-epsilons, or the later episodes get truncated")
    p.add_argument("--max-seconds", type=float, default=600,
                   help="wall-clock budget for the training loop; partial results are still written")
    p.add_argument("--torch-threads", type=int, default=0)
    p.add_argument("--name", default=None)
    p.add_argument("--out", default="output/harness")
    p.add_argument("--quiet", action="store_true")
    for param in _DQ.params:
        p.add_argument(f"--{param.name.replace('_', '-')}", default=None, type=_arg_type(param), help=param.label)
    return p


def _arg_type(param):
    if param.type == "bool":
        return lambda s: s.lower() == "true"
    return {"int": int, "float": float}.get(param.type, str)


def _apply_env(args) -> dict:
    os.environ["NETWORK_NAME"] = args.network
    os.environ["SAVE_MODEL"] = "false"
    os.environ.setdefault("DQ_ROUTER_DEBUG_EVERY", "1000000")
    os.environ.setdefault("DQ_DEBUG_FEATURES_EVERY", "1000000")
    return _DQ.resolve({p.name: getattr(args, p.name) for p in _DQ.params if getattr(args, p.name) is not None})


class _InProcessBus:
    def __init__(self):
        self.router = None
        self.broadcasts = 0

    def send(self, topic, payload):
        from common.config import TOPIC_MODEL_WEIGHTS
        if topic != TOPIC_MODEL_WEIGHTS:
            return
        self.broadcasts += 1
        if self.router is not None:
            self.router.apply_update(payload)

    def flush(self):
        pass

    def close(self):
        pass


class TrainDriver:
    def __init__(self, learner, poll_size: int):
        self._learner = learner
        self._poll_size = poll_size
        self._pending = 0
        self.series: list[dict] = []
        self._last: dict = {}

    def on_transition(self, exp: dict) -> None:
        self._learner.push(exp)
        self._pending += 1
        if self._pending >= self._poll_size:
            self.flush()

    def flush(self) -> None:
        n, self._pending = self._pending, 0
        if n == 0:
            return

        L = self._learner
        if L.hp["recent_only"]:
            warm = len(L._recent_buffer) >= L.hp["batch_size"]
        else:
            warm = (len(L._replay_buffer) >= L.hp["min_buffer_size"]
                    and len(L._recent_buffer) >= L.hp["batch_size"] // 2)
        if not warm:
            return

        for _ in range(L.hp["train_steps_per_cycle"]):
            loss, grad_norm, q_mean = L.train_step()
            self._last = {"loss": loss, "grad_norm": grad_norm, "q_mean": q_mean}
            L._maybe_update_target()
            if L._train_steps % L.hp["broadcast_every"] == 0:
                L._broadcast_weights()
                revisit_rate, _ = L.revisit_stats()
                self.series.append({
                    "train_steps": L._train_steps,
                    "experiences_seen": L._experiences_seen,
                    "loss": round(loss, 6),
                    "grad_norm": round(grad_norm, 4),
                    "q_mean": round(q_mean, 4),
                    "epsilon": round(L._epsilon, 4),
                    "revisit_rate": round(revisit_rate, 4),
                })

    @property
    def last(self) -> dict:
        return self._last


class DecisionAudit:
    def __init__(self, env, policy):
        import networkx as nx
        self._nx = nx
        self._env = env
        self._policy = policy
        self._rev = env.line_graph.reverse()
        self._dist_cache: dict[str, dict[str, float]] = {}
        self.reset()

    def reset(self) -> None:
        self.decisions = 0
        self.follows = 0
        self.detour_cost = 0.0

    def _dist(self, dest: str) -> dict[str, float]:
        if dest not in self._dist_cache:
            try:
                self._dist_cache[dest] = dict(
                    self._nx.single_source_dijkstra_path_length(self._rev, dest, weight="time"))
            except Exception:
                self._dist_cache[dest] = {}
        return self._dist_cache[dest]

    def __call__(self, cur, dest, vid):
        chosen = self._policy(cur, dest, vid)
        cands = self._env.connection_map.get(cur, [])
        if chosen is not None and len(cands) > 1:
            dist = self._dist(dest)
            big = float("inf")
            d_chosen = dist.get(chosen, big)
            d_best = min(dist.get(c, big) for c in cands)
            self.decisions += 1
            if d_chosen - d_best <= 3.0:  # ties count as following
                self.follows += 1
            elif d_chosen < big and d_best < big:
                self.detour_cost += d_chosen - d_best
        return chosen

    def stats(self) -> dict:
        if not self.decisions:
            return {"follows_pct": 0.0, "mean_detour_s": 0.0, "audited_decisions": 0}
        return {
            "follows_pct": round(100.0 * self.follows / self.decisions, 2),
            "mean_detour_s": round(self.detour_cost / self.decisions, 3),
            "audited_decisions": self.decisions,
        }


class DynamicDijkstra:
    """Shortest path under the observed snapshot, recomputed per broadcast"""

    def __init__(self, env, dyn):
        self._env = env
        self._dyn = dyn
        self.on_broadcast({e: {"speed": s} for e, s in zip(env.ordered_edges, env._freeflow_speed)})

    def on_broadcast(self, snapshot: dict) -> None:
        self._speeds = [snapshot[e]["speed"] for e in self._env.ordered_edges]
        self._times = self._dyn.edge_times(self._speeds)
        self._dist: dict = {}

    def __call__(self, cur, dest, vid):
        cands = self._env.connection_map.get(cur, [])
        if not cands:
            return None
        if dest not in self._dist:
            self._dist[dest] = self._dyn.seconds(self._speeds, dest)
        d, idx = self._dist[dest], self._env._idx
        return min(cands, key=lambda c: self._times[idx[c]] + d[idx[c]])


def _global_max_dist(line_graph) -> float:
    import networkx as nx
    gmax = 0.0
    for e in line_graph.nodes:
        lengths = nx.single_source_dijkstra_path_length(line_graph, e, weight="time")
        gmax = max(gmax, max(lengths.values(), default=0.0))
    return max(gmax, 1.0)


def _spearman(a, b) -> float:
    import numpy as np
    if len(a) < 3:
        return 0.0
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    ra -= ra.mean()
    rb -= rb.mean()
    denom = float(np.sqrt((ra ** 2).sum() * (rb ** 2).sum()))
    return float((ra * rb).sum() / denom) if denom else 0.0


def value_probe(router, env, dest_edges, dyn) -> dict:
    """Score Q against the static optimum and against the optimum under the router's current snapshot"""
    import networkx as nx
    import torch

    rev = env.line_graph.reverse()
    corrs, follows, total, q_spans = [], 0, 0, []
    dyn_corrs, dyn_follows = [], 0
    speeds = router._raw_speeds()
    times = dyn.edge_times(speeds)

    for dest in dest_edges:
        dist = dict(nx.single_source_dijkstra_path_length(rev, dest, weight="time"))
        dd = dyn.seconds(speeds, dest)
        with torch.no_grad():
            q = router._model(router._build_feature_tensor(dest),
                              router._data.edge_index).squeeze(-1)

        idx, opt, opt_dyn = [], [], []
        for e, i in router._edge_to_idx.items():
            if e in dist and e != dest:
                idx.append(i)
                opt.append(-(env.edge_map[e][3] + dist[e]))
                opt_dyn.append(-(times[env._idx[e]] + dd[env._idx[e]]))
        if len(idx) >= 3:
            qv = q[idx].cpu().numpy()
            corrs.append(_spearman(qv, opt))
            dyn_corrs.append(_spearman(qv, opt_dyn))
            q_spans.append(float(qv.max() - qv.min()))

        big = float("inf")
        for e in env.ordered_edges:
            cands = env.connection_map.get(e, [])
            if len(cands) < 2 or e == dest:
                continue
            chosen = max(cands, key=lambda c: q[router._edge_to_idx[c]].item())
            d_chosen = dist.get(chosen, big)
            d_best = min(dist.get(c, big) for c in cands)
            total += 1
            if d_chosen - d_best <= 3.0:
                follows += 1
            cost = lambda c: times[env._idx[c]] + dd[env._idx[c]]
            if cost(chosen) - min(cost(c) for c in cands) <= 3.0:
                dyn_follows += 1

    return {
        "probe_q_vs_optimal": round(sum(corrs) / len(corrs), 4) if corrs else 0.0,
        "probe_follows_pct": round(100.0 * follows / total, 2) if total else 0.0,
        "probe_q_vs_dynamic": round(sum(dyn_corrs) / len(dyn_corrs), 4) if dyn_corrs else 0.0,
        "probe_follows_dynamic_pct": round(100.0 * dyn_follows / total, 2) if total else 0.0,
        "probe_q_span": round(sum(q_spans) / len(q_spans), 4) if q_spans else 0.0,
        "probe_dests": len(dest_edges),
    }


def _write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2))


def main() -> None:
    args = build_parser().parse_args()
    hp = _apply_env(args)

    import random
    import torch
    from experiments.harness.graph_env import GraphTrafficEnv
    from algorithms.dijkstra.router import DijkstraRouter
    from common.config import NETWORK_FILE, SPEED_EMA_ALPHA, route_file_path
    from common.networking.dynamic_dist import DynamicDist

    if args.speed_ema is None:
        args.speed_ema = SPEED_EMA_ALPHA

    if args.torch_threads:
        torch.set_num_threads(args.torch_threads)
    random.seed(args.seed)
    torch.manual_seed(args.seed)

    env = GraphTrafficEnv(
        net_file=NETWORK_FILE,
        n_vehicles=args.vehicles,
        depart_period=args.depart_period,
        max_hops=args.max_hops,
        congestion_k=args.congestion_k,
        min_path_hops=args.min_path_hops,
        blind_traffic=args.blind_traffic,
        speed_ema=args.speed_ema,
        seed=args.seed,
    )
    eval_od = env.sample_od_pairs(args.eval_vehicles, seed=args.eval_seed)
    eval_epsilons = [float(x) for x in str(args.eval_epsilons).split(",") if x.strip() != ""]
    file_trips = env.load_route_file(route_file_path(args.network)) if args.route_file else None

    if args.jam_top_edges and args.route_file:
        from layer1_simulation.bottleneck import edge_usage
        usage = edge_usage(route_file_path(args.network))
        jam = [e for e, _ in sorted(usage.items(), key=lambda kv: -kv[1])[:args.jam_top_edges]]
        env.jam_edges = {e: args.jam_factor for e in jam}
        result_jam = {"edges": jam, "factor": args.jam_factor,
                      "share_of_shortest_paths": round(
                          sum(usage[e] for e in jam) / max(sum(usage.values()), 1), 3)}
    elif args.jam_top_edges:
        import collections
        import networkx as nx
        usage = collections.Counter()
        for start, dest in env.sample_od_pairs(300, seed=args.eval_seed + 1):
            try:
                for e in nx.shortest_path(env.line_graph, start, dest, weight="time")[:-1]:
                    usage[e] += 1
            except Exception:
                pass
        jam = [e for e, _ in usage.most_common(args.jam_top_edges)]
        env.jam_edges = {e: args.jam_factor for e in jam}
        result_jam = {"edges": jam, "factor": args.jam_factor,
                      "share_of_shortest_paths": round(
                          sum(usage[e] for e in jam) / max(sum(usage.values()), 1), 3)}
    else:
        result_jam = None
    dijkstra = DijkstraRouter(env.line_graph, env.edge_map, env.connection_map, {})

    def dijkstra_policy(cur, dest, vid):
        return dijkstra.find_next_edge(cur, dest, vid)

    dyn = DynamicDist(env.ordered_edges, env.edge_map, env.connection_map, _global_max_dist(env.line_graph), hp["min_ratio"])

    baseline_rng = random.Random(args.seed)

    def random_policy(cur, dest, vid):
        cands = env.connection_map.get(cur, [])
        return baseline_rng.choice(cands) if cands else None

    run_name = args.name or f"{args.policy}_{time.strftime('%Y%m%d_%H%M%S')}"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_name}.json"
    log = (lambda *a: None) if args.quiet else print

    log(f"\n=== baselines on {args.eval_vehicles} fixed eval trips (congestion_k={args.congestion_k}) ===")
    baselines = {}
    for label, pol in (("dijkstra", dijkstra_policy), ("dynamic_dijkstra", DynamicDijkstra(env, dyn)),
                       ("random", random_policy)):
        audited = DecisionAudit(env, pol)
        res = env.run_episode(audited, eval_od, on_broadcast=getattr(pol, "on_broadcast", None))
        baselines[label] = {**res.as_dict(), **audited.stats()}
        log(f"  {label:9s} arrived={res.arrival_rate:6.1%}  tt={res.mean_travel_time:8.1f}s  "
            f"hops_ratio={res.hops_ratio:5.2f}  looped={res.looped_pct:5.1f}%  "
            f"follows={baselines[label]['follows_pct']:5.1f}%")

    dijkstra_episode = None
    if file_trips:
        audited = DecisionAudit(env, dijkstra_policy)
        dijkstra_episode = {**env.run_episode(audited, file_trips).as_dict(), **audited.stats()}
        log(f"  dijkstra on the {len(file_trips)} route-file trips: "
            f"censTT={dijkstra_episode['censored_travel_time']:.1f}s  arrived={dijkstra_episode['arrived']}")

    dynamic_dijkstra_episode = None
    if file_trips:
        pol = DynamicDijkstra(env, dyn)
        audited = DecisionAudit(env, pol)
        dynamic_dijkstra_episode = {**env.run_episode(audited, file_trips, on_broadcast=pol.on_broadcast).as_dict(),
                                    **audited.stats()}
        log(f"  dynamic dijkstra on the {len(file_trips)} route-file trips: "
            f"censTT={dynamic_dijkstra_episode['censored_travel_time']:.1f}s  "
            f"arrived={dynamic_dijkstra_episode['arrived']}")

    result = {
        "run_name": run_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "args": vars(args),
        "hyperparams": hp,
        "network": os.path.basename(NETWORK_FILE),
        "jam": result_jam,
        "dijkstra_episode": dijkstra_episode,
        "dynamic_dijkstra_episode": dynamic_dijkstra_episode,
        "baselines": baselines,
        "evals": [],
        "train_episodes": [],
        "training_series": [],
    }

    if args.policy != "deep_q":
        _write(path, result)
        log(f"\nwrote {path}")
        return

    from algorithms.deep_q.router import DeepQRouter
    from algorithms.deep_q.learner import DeepQLearner

    bus = _InProcessBus()
    learner = DeepQLearner(bus, hp)
    router = DeepQRouter(env.line_graph, env.edge_map, env.connection_map, hp)
    bus.router = router
    router.set_epsilon(learner._epsilon)
    driver = TrainDriver(learner, poll_size=args.poll_size)

    def deep_q_policy(cur, dest, vid):
        return router.find_next_edge(cur, dest, vid)

    def on_broadcast(snapshot):
        router.update_traffic_state(snapshot)

    audit = DecisionAudit(env, deep_q_policy)
    probe_dests = sorted({d for _, d in eval_od})[:8]

    def evaluate(episode: int, eval_budget: float) -> list[dict]:
        learner._broadcast_weights()
        saved_eps = router.epsilon
        probe = value_probe(router, env, probe_dests, dyn)
        rows = []
        for eps in eval_epsilons:
            router.set_epsilon(eps)
            audit.reset()
            try:
                res = env.run_episode(audit, eval_od, on_broadcast=on_broadcast,
                                      deadline=time.time() + eval_budget)
            finally:
                router.set_epsilon(saved_eps)
            row = res.as_dict()
            row.update(audit.stats())
            row.update(probe)
            row.update({
                "episode": episode,
                "eval_epsilon": eps,
                "train_steps": learner._train_steps,
                "experiences_seen": learner._experiences_seen,
                "epsilon_train": round(learner._epsilon, 4),
                **{k: round(v, 5) for k, v in driver.last.items()},
            })
            rows.append(row)
        return rows

    log(f"\n=== training: {args.episodes} episodes x {args.vehicles} vehicles, "
        f"budget {args.max_seconds:.0f}s ===")
    log(f"{'ep':>4} {'exps':>7} {'steps':>6} {'evEps':>5} | {'arr%':>6} {'censTT':>8} "
        f"{'hops_r':>7} {'loop%':>6} {'follow%':>7} {'probe%':>7} {'corr':>6} | "
        f"{'loss':>8} {'|Q|':>7} {'wall':>6}")

    t0 = time.time()
    budget_end = t0 + args.max_seconds
    stopped_early = False

    for ep in range(1, args.episodes + 1):
        if time.time() > budget_end:
            stopped_early = True
            break
        train_od = file_trips or env.sample_od_pairs(args.vehicles)
        router._plans.clear()  # vehicle ids repeat across episodes
        router.set_epsilon(learner._epsilon)
        res = env.run_episode(deep_q_policy, train_od, on_transition=driver.on_transition,
                              on_broadcast=on_broadcast, deadline=budget_end)
        driver.flush()
        result["train_episodes"].append({"episode": ep, "train_steps": learner._train_steps, **res.as_dict()})
        if args.warm_start_reset:
            learner._replay_buffer._buffer.clear()
            learner._recent_buffer.clear()
            learner._target_net.load_state_dict(learner._online_net.state_dict())

        if ep % args.eval_every == 0 or ep == args.episodes:
            for row in evaluate(ep, args.eval_seconds):
                result["evals"].append(row)
                log(f"{ep:>4} {row['experiences_seen']:>7} {row['train_steps']:>6} "
                    f"{row['eval_epsilon']:>5.2f} | {row['arrival_rate']*100:>5.1f}% "
                    f"{row['censored_travel_time']:>8.1f} {row['hops_ratio']:>7.2f} "
                    f"{row['looped_pct']:>5.1f}% {row['follows_pct']:>6.1f}% "
                    f"{row['probe_follows_pct']:>6.1f}% {row['probe_q_vs_optimal']:>6.2f} | "
                    f"{row.get('loss', float('nan')):>8.4f} "
                    f"{row.get('q_mean', float('nan')):>7.2f} {time.time()-t0:>6.0f}s")
            _write(path, result)

    done_eps = result["train_episodes"][-1]["episode"] if result["train_episodes"] else 0
    if done_eps and (not result["evals"] or result["evals"][-1]["episode"] != done_eps):
        result["evals"].extend(evaluate(done_eps, args.eval_seconds))

    result["training_series"] = driver.series
    result["wall_seconds"] = round(time.time() - t0, 1)
    result["weight_broadcasts"] = bus.broadcasts
    result["stopped_early"] = stopped_early
    result["episodes_completed"] = len(result["train_episodes"])
    _write(path, result)

    d = baselines["dijkstra"]
    last = result["evals"][-1] if result["evals"] else {}
    ratio = (last.get("mean_travel_time", 0) / d["mean_travel_time"]) if d["mean_travel_time"] else 0
    log("\n--- summary ---")
    log(f"  dijkstra : arrived={d['arrival_rate']*100:5.1f}%  tt={d['mean_travel_time']:8.1f}s  "
        f"hops_ratio={d['hops_ratio']:.2f}")
    log(f"  random   : arrived={baselines['random']['arrival_rate']*100:5.1f}%  "
        f"tt={baselines['random']['mean_travel_time']:8.1f}s  "
        f"hops_ratio={baselines['random']['hops_ratio']:.2f}")
    log(f"  deep_q   : arrived={last.get('arrival_rate', 0)*100:5.1f}%  "
        f"tt={last.get('mean_travel_time', 0):8.1f}s  "
        f"hops_ratio={last.get('hops_ratio', 0):.2f}  ({ratio:.2f}x dijkstra)")
    log(f"  episodes={result['episodes_completed']}  transitions={learner._experiences_seen}  "
        f"grad_steps={learner._train_steps}  wall={result['wall_seconds']}s"
        f"{'  [BUDGET HIT]' if stopped_early else ''}")
    log(f"\nwrote {path}")


if __name__ == "__main__":
    main()
