import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError

import algorithms
from common.controls.control_base import StartRequest, algorithm_env
from experiment_dashboard.sa_tuner import _build_space, _fixed, _split
from layer1_simulation.metrics import MetricsStore


def _raises(fn, exc=ValueError):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"{fn} did not raise {exc.__name__}")


def test_registry_invariants():
    platform = {p.name for p in algorithms.PLATFORM_TUNABLES}
    for algo in algorithms.ALGORITHMS.values():
        names = [p.name for p in algo.params]
        assert len(names) == len(set(names)) and not platform & set(names), algo.name
        assert algo.resolve() == {p.name: p.default for p in algo.params}
        if algo.checkpoint:
            assert algo.learns and algorithms.checkpoint_path(algo.name).endswith(algo.checkpoint)
        d = algo.descriptor()
        assert json.loads(json.dumps(d)) == d
    assert algorithms.checkpoint_path("dijkstra") is None
    refs = {a.name for a in algorithms.ALGORITHMS.values() if a.reference}
    assert refs == {"dijkstra", "dynamic_dijkstra"}, refs


def test_resolve_rejects_bad_values():
    dq = algorithms.get("deep_q")
    _raises(lambda: algorithms.get("nope"))
    _raises(lambda: dq.resolve({"nope": 1}))
    _raises(lambda: dq.resolve({"gamma": 2.0}))
    _raises(lambda: dq.resolve({"batch_size": 3.5}))
    _raises(lambda: dq.resolve({"recent_only": "false"}))
    _raises(lambda: dq.resolve({"learning_rate": True}))
    _raises(lambda: dq.resolve({"device": "tpu"}))
    hp = dq.resolve({"batch_size": 128.0, "recent_only": False})
    assert hp["batch_size"] == 128 and isinstance(hp["batch_size"], int) and hp["recent_only"] is False


def test_start_request_forbids_old_keys():
    _raises(lambda: StartRequest(routing_mode="deep_q"), ValidationError)
    _raises(lambda: StartRequest(algorithm="deep_q", dq_gamma=0.9), ValidationError)
    _raises(lambda: StartRequest(algorithm="deep_q", hyperparams={"gamma": "x"}), ValidationError)
    _raises(lambda: StartRequest(algorithm="unknown"), ValidationError)


def test_algorithm_env_round_trip():
    req = StartRequest(algorithm="deep_q", hyperparams={"gamma": 0.95, "dynamic_dist": True})
    env = algorithm_env(req)
    old = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update(env)
        algo, hp = algorithms.resolve_env()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert algo.name == "deep_q" and hp == req.hyperparams and hp["gamma"] == 0.95 and hp["dynamic_dist"] is True


def test_sa_space_and_split():
    dq = algorithms.get("deep_q")
    space = _build_space(dq, [{"key": "learning_rate", "min": 1.0, "max": 1e-5},
                              {"key": "speed_ema_alpha", "min": 0.05, "max": 5.0},
                              {"key": "recent_only"},
                              {"key": "device"},
                              {"key": "nope"}])
    assert set(space) == {"learning_rate", "speed_ema_alpha", "recent_only"}, set(space)
    _, lo, hi = space["learning_rate"]
    assert lo == 1e-5 and hi == 1e-2
    assert space["speed_ema_alpha"][2] == 1.0
    assert space["recent_only"][1:] == (None, None)
    assert "speed_ema_alpha" not in _build_space(algorithms.get("dijkstra"), [{"key": "speed_ema_alpha"}])
    assert _split({"speed_ema_alpha": 0.2, "gamma": 0.9}) == ({"speed_ema_alpha": 0.2}, {"gamma": 0.9})
    platform, hp = _fixed({"fixed": {"speed_ema_alpha": 0.3, "gamma": 0.9, "learning_rate": 1e-4}}, space)
    assert platform == {} and hp == {"gamma": 0.9}


def test_queue_endpoint_validation():
    from experiment_dashboard.app import app
    client = app.test_client()
    for body in ({"algorithm": "nope"}, {"routing_mode": "deep_q"}, {"algorithm": "deep_q", "dq_gamma": 0.9},
                 {"algorithm": "deep_q", "hyperparams": {"gamma": 5}}):
        r = client.post("/api/queue", json=body)
        assert r.status_code == 400 and r.get_json()["error"], body
    r = client.post("/api/queue", json={"algorithm": "deep_q", "save_checkpoint": True})
    item = r.get_json()
    assert r.status_code == 201 and item["save_checkpoint"] is True
    assert item["config"]["hyperparams"] == algorithms.get("deep_q").resolve()
    r = client.post("/api/queue", json={"algorithm": "dijkstra", "save_checkpoint": True})
    assert r.get_json()["save_checkpoint"] is False
    client.delete("/api/queue/" + item["id"])


def test_metrics_store_learner_metrics():
    algo = algorithms.get("deep_q")
    with tempfile.TemporaryDirectory() as tmp:
        store = MetricsStore("deep_q_test", algo, tmp, config={"algorithm": "deep_q"})
        store.ingest_event({"type": "learner_metrics", "algorithm": "dijkstra", "step": 1, "values": {"loss": 1.0}})
        for step, loss in ((100, 4.0), (200, 2.0), (300, 3.0)):
            store.ingest_event({"type": "learner_metrics", "algorithm": "deep_q", "step": step,
                                "values": {"loss": loss},
                                "learner_state": {"loaded_checkpoint": step == 100}})
        store.write_final(10, 10.0, 1.0, {})
        d = json.loads((Path(tmp) / "deep_q_test.json").read_text())
    assert d["schema_version"] == 2
    assert [p["step"] for p in d["algorithm_metrics"]["series"]] == [100, 200, 300]
    assert d["algorithm_metrics"]["summary"] is None
    assert d["config"]["loaded_checkpoint"] is True


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
