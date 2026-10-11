import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NETWORK_NAME", "irregular_6x6_very_short_and_calm")

import networkx as nx
import numpy as np

import algorithms
from common.config import NETWORK_FILE, network_file_path
from common.networking.network import load_network
from common.networking.dynamic_dist import DynamicDist
from algorithms.dijkstra.router import DijkstraRouter
from algorithms.dynamic_dijkstra.router import DynamicDijkstraRouter

NETWORKS = [NETWORK_FILE, network_file_path("irregular_6x6_staged_medium")]
_HP = algorithms.get("dynamic_dijkstra").resolve()


def _close(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(b))


def _load(net):
    _, line_graph, edge_map, connection_map = load_network(net)
    return line_graph, edge_map, connection_map


def _freeflow(edge_map, e):
    return (edge_map[e][2] / edge_map[e][3]) if edge_map[e][3] > 0 else 14.0


def _random_snapshot(edge_map, seed):
    rng = np.random.default_rng(seed)
    edges = list(edge_map)
    snap = {e: {"speed": _freeflow(edge_map, e) * rng.uniform(0.0, 1.2)} for e in edges}
    snap[edges[3]]["speed"] = 0.0
    for e in edges[::7]:
        snap.pop(e)
    return snap


def _oracle_graph(router, connection_map):
    _, times, _ = router._snap
    g = nx.DiGraph()
    for a, succ in connection_map.items():
        for b in succ:
            if a in router._idx and b in router._idx:
                g.add_edge(a, b, w=float(times[router._idx[b]]))
    return g


def _chosen_cost(router, choice, dest):
    speeds, times, _ = router._snap
    i = router._idx[choice]
    return float(times[i] + router._dyn.seconds(speeds, dest)[i])


def test_free_flow_equals_static_dijkstra():
    for net in NETWORKS:
        line_graph, edge_map, connection_map = _load(net)
        router = DynamicDijkstraRouter(line_graph, edge_map, connection_map, _HP)
        static = DijkstraRouter(line_graph, edge_map, connection_map, {})
        edges = list(edge_map)
        checked = 0
        for cur in edges[::3]:
            for dest in edges[::5]:
                if cur == dest:
                    continue
                choice = router.find_next_edge(cur, dest)
                try:
                    opt = nx.shortest_path_length(line_graph, cur, dest, weight="time")
                except nx.NetworkXNoPath:
                    assert choice is None
                    continue
                assert choice is not None
                assert _close(_chosen_cost(router, choice, dest), opt), (cur, dest)
                s = static.find_next_edge(cur, dest)
                assert _close(_chosen_cost(router, s, dest), opt), (cur, dest)
                checked += 1
        assert checked > 50


def test_matches_networkx_under_observed_speeds():
    for net in NETWORKS:
        line_graph, edge_map, connection_map = _load(net)
        router = DynamicDijkstraRouter(line_graph, edge_map, connection_map, _HP)
        for seed in (0, 1, 2):
            router.update_traffic_state(_random_snapshot(edge_map, seed))
            g = _oracle_graph(router, connection_map)
            edges = list(edge_map)
            for cur in edges[::4]:
                for dest in edges[::6]:
                    if cur == dest:
                        continue
                    choice = router.find_next_edge(cur, dest)
                    try:
                        opt = nx.shortest_path_length(g, cur, dest, weight="w")
                    except (nx.NetworkXNoPath, nx.NodeNotFound):
                        assert choice is None
                        continue
                    assert _close(_chosen_cost(router, choice, dest), opt), (net, seed, cur, dest)


def test_avoids_a_jammed_next_edge():
    line_graph, edge_map, connection_map = _load(NETWORKS[1])
    router = DynamicDijkstraRouter(line_graph, edge_map, connection_map, _HP)
    edges = list(edge_map)
    for cur in edges:
        if len(connection_map.get(cur, [])) < 2:
            continue
        for dest in edges:
            if dest == cur or dest in connection_map[cur]:
                continue
            router.update_traffic_state({})
            first = router.find_next_edge(cur, dest)
            if first is None:
                continue
            router.update_traffic_state({first: {"speed": 0.0}})
            if router.find_next_edge(cur, dest) not in (first, None):
                return
    raise AssertionError("no case where jamming the free-flow choice changed the decision")


def test_guards():
    line_graph, edge_map, connection_map = _load(NETWORK_FILE)
    router = DynamicDijkstraRouter(line_graph, edge_map, connection_map, _HP)
    e = next(iter(edge_map))
    assert router.find_next_edge(e, e) is None
    assert router.find_next_edge("no_such_edge", e) is None
    assert router.find_next_edge(e, "no_such_edge") is None
    dead_end = next((x for x in edge_map if not connection_map.get(x)), None)
    if dead_end is not None:
        assert router.find_next_edge(dead_end, e) is None


def test_unreachable_destination_returns_none():
    for net in NETWORKS:
        line_graph, edge_map, connection_map = _load(net)
        router = DynamicDijkstraRouter(line_graph, edge_map, connection_map, _HP)
        edges = list(edge_map)
        for cur in edges:
            for dest in edges:
                if cur != dest and not nx.has_path(line_graph, cur, dest):
                    assert router.find_next_edge(cur, dest) is None
                    break


def test_cache_per_snapshot_and_destination():
    line_graph, edge_map, connection_map = _load(NETWORK_FILE)
    router = DynamicDijkstraRouter(line_graph, edge_map, connection_map, _HP)
    calls = []
    orig = router._dyn.seconds
    router._dyn.seconds = lambda s, d: calls.append(d) or orig(s, d)
    edges = list(edge_map)
    cur = next(e for e in edges if connection_map.get(e))
    dest = next(e for e in reversed(edges) if e != cur)
    router.find_next_edge(cur, dest)
    router.find_next_edge(cur, dest)
    assert len(calls) == 1
    other = next(e for e in edges if e not in (cur, dest))
    router.find_next_edge(cur, other)
    assert len(calls) == 2
    router.update_traffic_state(_random_snapshot(edge_map, 5))
    router.find_next_edge(cur, dest)
    assert len(calls) == 3


def test_snapshot_swap_is_atomic():
    line_graph, edge_map, connection_map = _load(NETWORK_FILE)
    router = DynamicDijkstraRouter(line_graph, edge_map, connection_map, _HP)
    old = router._snap
    router.update_traffic_state(_random_snapshot(edge_map, 1))
    assert router._snap is not old and router._snap[2] == {}
    assert old[2] is not router._snap[2]


def test_matches_harness_policy():
    from experiments.harness.train_graph import DynamicDijkstra, _global_max_dist

    line_graph, edge_map, connection_map = _load(NETWORKS[1])
    edges = list(edge_map)

    class _Env:
        ordered_edges = edges
        _idx = {e: i for i, e in enumerate(edges)}
        _freeflow_speed = [_freeflow(edge_map, e) for e in edges]

    _Env.connection_map = connection_map
    harness = DynamicDijkstra(_Env, DynamicDist(edges, edge_map, connection_map, _global_max_dist(line_graph)))
    router = DynamicDijkstraRouter(line_graph, edge_map, connection_map, _HP)
    for seed in (0, 3):
        snap = _random_snapshot(edge_map, seed)
        harness.on_broadcast({e: {"speed": snap.get(e, {}).get("speed", _freeflow(edge_map, e))} for e in edges})
        router.update_traffic_state(snap)
        for cur in edges[::3]:
            for dest in edges[::7]:
                if cur == dest:
                    continue
                r = router.find_next_edge(cur, dest)
                if r is None:
                    continue
                h = harness(cur, dest, "v")
                assert _close(_chosen_cost(router, h, dest), _chosen_cost(router, r, dest)), (seed, cur, dest)


def test_registered_as_non_learning_snapshot_reader():
    algo = algorithms.get("dynamic_dijkstra")
    assert not algo.learns and algo.needs_snapshot and algo.checkpoint is None
    assert algorithms.get("deep_q").learns


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except Exception as exc:
                failed += 1
                print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failed else 0)
