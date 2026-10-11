"""Router and learner must build identical features from identical state"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("NETWORK_NAME", "irregular_6x6_very_short_and_calm")
os.environ.setdefault("SAVE_MODEL", "false")
os.environ.setdefault("DQ_DEBUG_FEATURES_EVERY", "100000000")
os.environ.setdefault("DQ_ROUTER_DEBUG_EVERY", "100000000")

import numpy as np
import torch

import algorithms
from common.config import NETWORK_FILE
from common.networking.network import load_network
from algorithms.deep_q.router import DeepQRouter
from algorithms.deep_q.learner import DeepQLearner


class _NullProducer:
    def send(self, *a, **k):
        pass

    def flush(self):
        pass


def _setup(dynamic_dist=None):
    _, line_graph, edge_map, connection_map = load_network(NETWORK_FILE)
    hp = algorithms.get("deep_q").resolve({} if dynamic_dist is None else {"dynamic_dist": dynamic_dist})
    router = DeepQRouter(line_graph, edge_map, connection_map, hp)
    learner = DeepQLearner(_NullProducer(), hp)
    return router, learner, edge_map


def _learner_features(learner, speeds, counts, dest_edge):
    sc = torch.tensor(speeds, dtype=torch.float32, device=learner._device)
    cc = torch.tensor(counts, dtype=torch.float32, device=learner._device)
    speed = (sc / learner._freeflow).clamp(max=1.0)
    count = cc / 30.0
    dist = learner._get_dist_tensor(dest_edge)
    static = learner._data.x
    cols = [static[:, 0], static[:, 1], speed, count, dist]
    if learner.n_features == 6:
        # the call train_step makes, on a float32 row of its batch array
        cols.append(learner._get_dyn_tensor(np.asarray(speeds, dtype=np.float32), dest_edge))
    return torch.stack(cols, dim=1)


def _random_state(edge_map, edges, seed=0):
    rng = np.random.default_rng(seed)
    freeflow = [(edge_map[e][2] / edge_map[e][3]) if edge_map[e][3] > 0 else 14.0 for e in edges]
    speeds = [f * rng.uniform(0.0, 1.2) for f in freeflow]
    speeds[3] = 0.0
    counts = [float(rng.integers(0, 25)) for _ in edges]
    return speeds, counts


def _router_learner_match(dynamic_dist):
    router, learner, edge_map = _setup(dynamic_dist)
    edges = learner._ordered_edges
    speeds, counts = _random_state(edge_map, edges)
    router.update_traffic_state({e: {"speed": s, "count": c} for e, s, c in zip(edges, speeds, counts)})
    for dest in (edges[7], edges[20], edges[-1]):
        x_router = router._build_feature_tensor(dest)
        x_learner = _learner_features(learner, speeds, counts, dest).cpu()
        x_probe = learner._probe_features(dest, {"speeds": speeds, "counts": counts}).cpu()
        assert x_router.shape == x_learner.shape == (len(edges), 6 if dynamic_dist else 5)
        torch.testing.assert_close(x_router, x_learner, rtol=1e-5, atol=1e-6)
        torch.testing.assert_close(x_router, x_probe, rtol=1e-5, atol=1e-6)


def _roundtrip(dynamic_dist):
    router, learner, _ = _setup(dynamic_dist)
    payload = {k: v.tolist() for k, v in learner._online_net.state_dict().items()}
    assert router.apply_update({"delta": payload, "n_features": learner.n_features})

    dest = learner._ordered_edges[7]
    router.update_traffic_state({})
    x = router._build_feature_tensor(dest)
    learner._online_net.eval()
    with torch.no_grad():
        q_router = router._model(x, router._data.edge_index)
        q_learner = learner._online_net(x.to(learner._device), learner._data.edge_index).cpu()
    learner._online_net.train()
    torch.testing.assert_close(q_router, q_learner, rtol=1e-4, atol=1e-5)


def test_edge_order_matches():
    router, learner, edge_map = _setup()
    assert router._ordered_edges == learner._ordered_edges
    assert list(edge_map) == learner._ordered_edges


def test_global_max_dist_matches():
    router, learner, _ = _setup()
    assert abs(router._global_max_dist - learner._global_max_dist) < 1e-6
    assert abs(router._log_dist_denom - learner._log_dist_denom) < 1e-6


def test_default_is_six_features():
    router, learner, _ = _setup()
    assert router.n_features == learner.n_features == 6


def test_feature_tensors_match():
    router, learner, edge_map = _setup()
    rng = np.random.default_rng(0)
    edges = learner._ordered_edges
    dest = edges[7]

    freeflow = [(edge_map[e][2] / edge_map[e][3]) if edge_map[e][3] > 0 else 14.0 for e in edges]
    speeds = [f * rng.uniform(0.1, 1.0) for f in freeflow]
    counts = [float(rng.integers(0, 25)) for _ in edges]

    router.update_traffic_state(
        {e: {"speed": s, "count": c} for e, s, c in zip(edges, speeds, counts)}
    )
    x_router = router._build_feature_tensor(dest)
    x_learner = _learner_features(learner, speeds, counts, dest).cpu()

    assert x_router.shape == x_learner.shape
    torch.testing.assert_close(x_router, x_learner, rtol=1e-5, atol=1e-6)


def test_unseen_edges_default_to_free_flow():
    router, learner, _ = _setup()
    router.update_traffic_state({})
    x = router._build_feature_tensor(learner._ordered_edges[7])
    assert torch.allclose(x[:, 2], torch.ones_like(x[:, 2]))


def test_weight_roundtrip_through_json_shape():
    _roundtrip(None)


def test_flag_off_keeps_five_features():
    router, learner, _ = _setup(False)
    assert router.n_features == learner.n_features == 5
    assert router._model.conv1.lin.weight.shape[1] == 5
    assert learner._online_net.conv1.lin.weight.shape[1] == 5


def test_sixth_column():
    router, learner, _ = _setup(True)
    assert router.n_features == learner.n_features == 6
    assert learner._online_net.conv1.lin.weight.shape[1] == 6
    router.update_traffic_state({})
    x = router._build_feature_tensor(learner._ordered_edges[7])
    assert x.shape == (len(learner._ordered_edges), 6)
    assert torch.isfinite(x).all()


def test_dynamic_equals_static_at_free_flow():
    router, learner, _ = _setup(True)
    router.update_traffic_state({})
    for dest in learner._ordered_edges[::9]:
        x = router._build_feature_tensor(dest)
        # unreachable edges: static reads 1.0, dynamic gmax / min_ratio
        reachable = x[:, 5] <= 1.0 + 1e-5
        assert reachable.sum() > 3
        torch.testing.assert_close(x[reachable, 5], x[reachable, 4], rtol=1e-5, atol=1e-5)
        assert torch.allclose(x[~reachable, 4], torch.ones_like(x[~reachable, 4]))


def test_dynamic_distance_grows_under_a_jam():
    router, learner, _ = _setup(True)
    edges = learner._ordered_edges
    dest = edges[7]
    router.update_traffic_state({})
    free = router._build_feature_tensor(dest)[:, 5].clone()
    router.update_traffic_state({e: {"speed": 0.0} for e in edges})
    jam = router._build_feature_tensor(dest)[:, 5]
    assert (jam >= free - 1e-6).all() and (jam > free + 1e-3).any()


def test_router_learner_dynamic_parity_random_speeds():
    _router_learner_match(True)


def test_router_learner_parity_random_speeds_flag_off():
    _router_learner_match(False)


def test_weight_roundtrip_f6():
    _roundtrip(True)


def test_router_drops_mismatched_weights():
    router, _, _ = _setup(True)
    _, learner5, _ = _setup(False)
    before = {k: v.clone() for k, v in router._model.state_dict().items()}
    payload = {k: v.tolist() for k, v in learner5._online_net.state_dict().items()}
    assert not router.apply_update({"delta": payload, "n_features": 5})
    for k, v in router._model.state_dict().items():
        assert torch.equal(v, before[k])


def _check_mismatch_starts_fresh(saved_dyn, loading_dyn):
    _, saver, _ = _setup(saved_dyn)
    saver._train_steps = 1234
    _, loader, _ = _setup(loading_dyn)
    online = {k: v.clone() for k, v in loader._online_net.state_dict().items()}
    target = {k: v.clone() for k, v in loader._target_net.state_dict().items()}
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ckpt.pt"
        saver.save_checkpoint(path)
        assert torch.load(path)["n_features"] == saver.n_features
        try:
            loader.load_checkpoint(path)
        except Exception:
            pass
        else:
            raise AssertionError("mismatched checkpoint loaded")
    assert loader._train_steps == 0
    for k, v in loader._online_net.state_dict().items():
        assert torch.equal(v, online[k])
    for k, v in loader._target_net.state_dict().items():
        assert torch.equal(v, target[k])


def test_mismatched_checkpoint_5_to_6_starts_fresh():
    _check_mismatch_starts_fresh(False, True)


def test_mismatched_checkpoint_6_to_5_starts_fresh():
    _check_mismatch_starts_fresh(True, False)


def test_matching_checkpoint_loads_both_nets():
    _, saver, _ = _setup(True)
    saver._train_steps = 77
    _, loader, _ = _setup(True)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ckpt.pt"
        saver.save_checkpoint(path)
        loader.load_checkpoint(path)
    assert loader._train_steps == 77
    saved = saver._online_net.state_dict()
    for net in (loader._online_net, loader._target_net):
        for k, v in net.state_dict().items():
            assert torch.equal(v, saved[k])


def test_train_step_runs_with_dynamic_dist():
    _, learner, edge_map = _setup(True)
    edges = learner._ordered_edges
    conn = learner._connection_map
    rng = np.random.default_rng(1)
    for k in range(200):
        prev = edges[k % len(edges)]
        if not conn.get(prev):
            continue
        speeds, counts = _random_state(edge_map, edges, seed=k)
        learner.push({
            "vehicle_id": f"v{k}", "prev_edge": prev, "new_edge": conn[prev][0],
            "state_before": {"speeds": speeds, "counts": counts}, "action": conn[prev][0],
            "travel_time": float(rng.uniform(5, 60)), "state_after": {"speeds": speeds, "counts": counts},
            "dest_edge": edges[7], "sim_time": float(k), "is_revisit": False, "revisit_count": 0,
        })
    loss, _, _ = learner.train_step()
    assert np.isfinite(loss) and learner._train_steps == 1


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
