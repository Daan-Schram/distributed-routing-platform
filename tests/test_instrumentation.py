import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError

import algorithms
from common.controls.control_base import StartRequest
from layer1_simulation.latency_tracker import LatencyTracker
from layer1_simulation.metrics import MetricsStore


def _write(store: MetricsStore, tmp: str, **kw) -> dict:
    store.write_final(10, 10.0, 1.0, {}, **kw)
    return json.loads((Path(tmp) / f"{store.run_id}.json").read_text())


def test_decision_latency_split():
    with tempfile.TemporaryDirectory() as tmp:
        store = MetricsStore("split_test", algorithms.get("dijkstra"), tmp)
        store.ingest_event({"type": "decision_latency", "latency_s": 0.01})
        for i in range(1, 21):
            store.ingest_event({"type": "decision_latency", "latency_s": 0.01 * i,
                                "transport_s": 0.002 * i, "prep_s": 0.0001 * i, "compute_s": 0.001})
        d = _write(store, tmp)
    split = d["decision_latency_split"]
    assert d["decision_latency"]["mean"] is not None
    assert split["compute_s"] == {"mean": 0.001, "p95": 0.001, "n": 20}
    assert split["transport_s"] == {"mean": 0.021, "p95": 0.04, "n": 20}
    assert split["prep_s"]["p95"] == 0.002
    assert "throttle" not in d


def test_decision_latency_split_empty():
    with tempfile.TemporaryDirectory() as tmp:
        store = MetricsStore("split_empty", algorithms.get("dijkstra"), tmp)
        store.ingest_event({"type": "decision_latency", "latency_s": 0.01})
        d = _write(store, tmp)
    assert d["decision_latency_split"] == {k: {"mean": None, "p95": None, "n": 0}
                                           for k in ("transport_s", "prep_s", "compute_s")}


def test_throttle_stats():
    t = LatencyTracker()
    assert t.throttle_stats()["step_delay_s"]["mean"] is None
    with mock.patch("layer1_simulation.latency_tracker.time.sleep") as sleep:
        t.wait()
        t._step_delay_s = 0.01
        t.set_training_pressure(300, 100)
        t.wait()
        t.set_training_pressure(50, 100)
        t.wait()
    s = t.throttle_stats()
    assert s["steps"] == 3 and sleep.call_count == 2
    assert s["training_delay_s"] == {"mean": round(0.25 / 3, 5), "max": 0.25, "total": 0.25}
    assert s["step_delay_s"]["max"] == 0.01 and s["step_delay_s"]["total"] == 0.02
    with tempfile.TemporaryDirectory() as tmp:
        d = _write(MetricsStore("throttle_test", algorithms.get("dijkstra"), tmp), tmp, throttle=s)
    assert d["throttle"] == s


def test_start_request_log_proxies():
    assert StartRequest().log_proxies is None
    assert StartRequest(telemetry=True, log_proxies=False).log_proxies is False
    try:
        StartRequest(log_proxy=False)
    except ValidationError:
        return
    raise AssertionError("unknown key accepted")


def test_queue_log_proxies_effective():
    import experiment_dashboard.app as dash
    for cfg, expected in (({"telemetry": False}, False), ({"telemetry": True}, True),
                          ({"telemetry": True, "log_proxies": False}, False),
                          ({"telemetry": False, "log_proxies": True}, True)):
        item = {"id": "x", "config": {"algorithm": "dijkstra", **cfg}, "group_id": "g",
                "repeat_done": 0, "repeats": 1}
        with mock.patch.object(dash, "_run_startup") as run, \
                mock.patch.object(dash.threading, "Thread") as thread:
            dash._queue_start_run(item)
        args = thread.call_args.kwargs.get("args") or thread.call_args.args[1]
        assert args[0]["log_proxies"] is expected, (cfg, args[0])
        assert dash._instr["log_proxies"] is expected
    dash.experiment_running = dash._startup_in_progress = False


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
