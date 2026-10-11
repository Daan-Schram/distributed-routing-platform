import collections
import glob as glob_module
import json
import mimetypes
import os
import re
import sys
import threading
import time
import uuid as _uuid_mod
from datetime import datetime as _dt
from pathlib import Path

import httpx
from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import algorithms
from common.config import (
    KAFKA_BOOTSTRAP, TOPIC_RESOURCE_METRICS,
    AGENT_LAYER1_URL, AGENT_LAYER2_URL, AGENT_LAYER3_URL, AGENT_LAYER4_URL,
    AGENT_LAYER3_EXTRA_URLS, AGENT_API_KEY, EXPERIMENTS_DIR, STATE_DIR, TRAJECTORIES_DIR,
    INSTRUMENTATION_DIR, network_file_path, route_file_path,
    list_available_networks_with_meta,
)
from common.controls.control_base import StartRequest
from common.networking.network import load_edge_geometry
from experiment_dashboard.sa_tuner import SimulatedAnnealingTuner

mimetypes.add_type("text/javascript", ".js")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CHECKPOINT_PATHS = {a.name: Path(algorithms.checkpoint_path(a.name)) for a in algorithms.ALGORITHMS.values() if a.checkpoint}

def _agent_headers() -> dict:
    return {"X-API-Key": AGENT_API_KEY} if AGENT_API_KEY else {}

app = Flask(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METRICS_DIR = EXPERIMENTS_DIR

experiment_running: bool = False
_startup_in_progress: bool = False
_current_config: dict = {}

# Experiment generation counter, SSE compare their version to prevent old logs
_experiment_gen: int = 0

# Set by /stop to tell _run_startup to abort
_startup_stop = threading.Event()

sim_log_lines = collections.deque(maxlen=1000)
kafka_log_lines = collections.deque(maxlen=500)
faust_log_lines = collections.deque(maxlen=500)
learner_log_lines = collections.deque(maxlen=500)
resource_events = collections.deque(maxlen=200)
resource_event_seq = 0
log_lock = threading.Lock()

_log_totals: dict[str, int] = {"sim": 0, "faust": 0, "kafka": 0, "learner": 0}
_deque_map: dict[str, collections.deque] = {
    "sim": sim_log_lines,
    "faust": faust_log_lines,
    "kafka": kafka_log_lines,
    "learner": learner_log_lines,
}


def _log_append(key: str, line: str) -> None:
    _deque_map[key].append(line)
    _log_totals[key] += 1


_instr_lock = threading.Lock()


def _new_instr(req_body: dict | None = None) -> dict:
    req_body = req_body or {}
    return {
        "queue_item_id": req_body.get("queue_item_id"),
        "repeat_index": req_body.get("repeat_index"),
        "algorithm": req_body.get("algorithm"),
        "network_name": req_body.get("network_name"),
        "telemetry": req_body.get("telemetry", True),
        "log_proxies": req_body.get("log_proxies", req_body.get("telemetry", True)),
        "started_at": time.time(),
        "_t0": time.monotonic(),
        "startup": {"steps": [], "total_s": None, "completed": False},
        "run": {},
        "stop": None,
        "sse_in": {},
        "sse_out": {"events": 0, "bytes": 0, "streams_opened": 0},
    }


_instr: dict = _new_instr()


def _instr_snapshot(run: dict) -> dict:
    with _instr_lock:
        snap = json.loads(json.dumps({k: v for k, v in run.items() if not k.startswith("_")}))
    snap["elapsed_s"] = round(time.monotonic() - run["_t0"], 3)
    return snap


def _write_instrumentation(run: dict) -> None:
    if not run.get("queue_item_id"):
        return
    try:
        os.makedirs(INSTRUMENTATION_DIR, exist_ok=True)
        path = os.path.join(INSTRUMENTATION_DIR, f"{run['queue_item_id']}_r{run['repeat_index']}.json")
        with open(path, "w") as f:
            json.dump({**_instr_snapshot(run), "written_at": time.time()}, f, indent=2)
    except Exception as e:
        print(f"[instrumentation] write failed: {e}")

# Stop-events for the log-proxy daemon threads, it is keyed by agent URL
_proxy_stop_events: dict[str, threading.Event] = {}


def _proxy_log_stream(
        agent_url: str,
        target_deque: collections.deque,
        stop_event: threading.Event,
        gen: int,
        label: str = "",
        log_key: str = "",
) -> None:
    """
    Daemon thread: pull SSE lines from agent logs and add to target_deque
    Reconnects automatically after errors until stop_event
    A read-timeout of 10 s ensures the stop_event is always checked
    """
    prefix = f"[{label}] " if label else ""
    stat_key = label or log_key or agent_url
    while not stop_event.is_set():
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=5, read=10, write=5, pool=5), headers=_agent_headers()) as client:
                with client.stream("GET", f"{agent_url}/logs/stream") as resp:
                    for line in resp.iter_lines():
                        if stop_event.is_set():
                            return
                        if _experiment_gen == gen:
                            with _instr_lock:
                                c = _instr["sse_in"].setdefault(stat_key, {"lines": 0, "bytes": 0})
                                c["lines"] += 1 if line.startswith("data: ") else 0
                                c["bytes"] += len(line.encode("utf-8")) + 1
                        if line.startswith("data: "):
                            with log_lock:
                                if _experiment_gen == gen:
                                    if log_key:
                                        _log_append(log_key, prefix + line[6:])
                                    else:
                                        target_deque.append(prefix + line[6:])
        except httpx.ReadTimeout:
            pass  # Happens when agent is idle
        except Exception:
            pass
        if not stop_event.is_set():
            time.sleep(1)


def _stop_all_proxies() -> None:
    """Signal all running proxy threads to stop"""
    for ev in _proxy_stop_events.values():
        ev.set()
    _proxy_stop_events.clear()


def _start_one_proxy(agent_url: str, target_deque: collections.deque, gen: int, label: str = "", log_key: str = "") -> None:
    """Start one proxy thread for the given agent"""
    ev = threading.Event()
    _proxy_stop_events[agent_url] = ev
    threading.Thread(
        target=_proxy_log_stream,
        args=(agent_url, target_deque, ev, gen, label, log_key),
        daemon=True,
    ).start()


def _make_multiplexed_sse_generator(my_gen):
    """Single SSE stream that contains lines from all 4 log panels and resources"""
    panel_keys = ["sim", "faust", "kafka", "learner"]
    def generate():
        run = _instr
        with _instr_lock:
            run["sse_out"]["streams_opened"] += 1
        for chunk in _raw():
            with _instr_lock:
                run["sse_out"]["events"] += 1 if chunk.startswith("data: ") else 0
                run["sse_out"]["bytes"] += len(chunk.encode("utf-8"))
            yield chunk

    def _raw():
        sent_total = {name: 0 for name in panel_keys}
        res_seq = -1
        hb = 0
        # Tell the browser to clear its log panels for this new stream
        yield f"data: {json.dumps({'p': 'clear'})}\n\n"
        while True:
            if _experiment_gen != my_gen:
                return
            had_data = False
            for name in panel_keys:
                with log_lock:
                    current_total = _log_totals[name]
                    lines = list(_deque_map[name])
                if current_total > sent_total[name]:
                    num_new = current_total - sent_total[name]
                    new = lines[max(0, len(lines) - num_new):]
                    for line in new:
                        yield f"data: {json.dumps({'p': name, 'l': line})}\n\n"
                        had_data = True
                    sent_total[name] = current_total
            with log_lock:
                events = list(resource_events)
            for ev in events:
                if ev.get("_seq", 0) > res_seq:
                    res_seq = ev["_seq"]
                    yield f"data: {json.dumps({'p': 'res', 'd': ev})}\n\n"
                    had_data = True
            hb += 1
            if hb >= 50:
                hb = 0
                yield ": hb\n\n"
            time.sleep(0.1)
    return generate


# Global cache for agent statuses, preventing blocking Flask threads
_cached_agent_statuses = {
    "sim": "stopped",
    "faust": "stopped",
    "learner": "stopped",
    "kafka": "— polling…",
    "kafka_stable": False
}

def _poll_all_agent_statuses() -> None:
    """Background thread to poll agents and Kafka periodically"""
    global experiment_running
    while True:
        # 1. Poll sumo layer
        try:
            r = httpx.get(f"{AGENT_LAYER1_URL}/status", timeout=2, headers=_agent_headers())
            s = r.json().get("status", "stopped")
            _cached_agent_statuses["sim"] = s
            if experiment_running and not _startup_in_progress and s == "stopped":
                experiment_running = False
        except Exception:
            _cached_agent_statuses["sim"] = "stopped"

        # 2. Poll actors + learner
        for key, url in [("faust", AGENT_LAYER3_URL), ("learner", AGENT_LAYER4_URL)]:
            try:
                r = httpx.get(f"{url}/status", timeout=2, headers=_agent_headers())
                _cached_agent_statuses[key] = r.json().get("status", "stopped")
            except Exception:
                _cached_agent_statuses[key] = "stopped"

        # 3. Poll Kafka
        try:
            r = httpx.get(f"{AGENT_LAYER2_URL}/kafka-consumer-group", timeout=3, headers=_agent_headers())
            out = r.json().get("output", "(empty)")
            _cached_agent_statuses["kafka"] = out
            _cached_agent_statuses["kafka_stable"] = bool(re.search(r"Stable", out, re.IGNORECASE))
        except Exception as e:
            _cached_agent_statuses["kafka"] = f"Error reaching Kafka agent: {e}"
            _cached_agent_statuses["kafka_stable"] = False

        time.sleep(3)


threading.Thread(target=_poll_all_agent_statuses, daemon=True).start()


_experiment_queue: list[dict] = []
_queue_lock = threading.Lock()
_group_durations: dict[str, list[float]] = {}  # group_id -> wall time in seconds per completed repeat, including startup
_queue_active: bool = False
_queue_stop = threading.Event()


def _queue_start_run(item: dict) -> None:
    """Launch a single run from a queue item"""
    global experiment_running, _startup_in_progress, _experiment_gen, _current_config, _instr

    cfg = item["config"]
    req_body = {**cfg, "group_id": item["group_id"]}
    is_first = item["repeat_done"] == 0
    wipe_first = item.get("reset_checkpoint_first", False)
    saves = item.get("save_checkpoint", False)
    if is_first:
        load = item.get("load_checkpoint_first", False) or (saves and not wipe_first)
    else:
        load = saves or item.get("load_checkpoint_first", False)
    req_body["load_checkpoint"] = load
    req_body["reset_checkpoint"] = is_first and wipe_first and not load
    req_body["save_model"] = item.get("save_checkpoint", False)
    req_body["restart_faust"] = True  # always
    req_body["queue_item_id"] = item["id"]
    req_body["repeat_index"] = item["repeat_done"] + 1
    req_body["repeats"] = item["repeats"]
    if cfg.get("sumo_seed") is not None:
        req_body["sumo_seed"] = int(cfg["sumo_seed"]) + item["repeat_done"]
    req_body["warm_start"] = load
    req_body["checkpoint_exists_before"] = None
    req_body["checkpoint_train_steps_before"] = None
    if load:
        ck = _checkpoint_status(cfg["algorithm"])
        req_body["checkpoint_exists_before"] = ck.get("exists")
        req_body["checkpoint_train_steps_before"] = ck.get("train_steps")

    extra_faust_urls = [u.strip() for u in AGENT_LAYER3_EXTRA_URLS.split(",") if u.strip()]
    req_body["num_actor_vms"] = 1 + len(extra_faust_urls)
    req_body.setdefault("wait", True)

    req_body["telemetry"] = bool(cfg.get("telemetry", True))
    _lp = cfg.get("log_proxies")
    req_body["log_proxies"] = req_body["telemetry"] if _lp is None else bool(_lp)
    _current_config = {**req_body}
    _instr = _new_instr(req_body)
    if _docker_monitor is not None:
        _docker_monitor.paused = not req_body.get("telemetry", True)

    with log_lock:
        sim_log_lines.clear()
        kafka_log_lines.clear()
        faust_log_lines.clear()
        learner_log_lines.clear()
        resource_events.clear()
        for k in _log_totals:
            _log_totals[k] = 0

    _experiment_gen += 1
    _startup_stop.clear()
    _startup_in_progress = True
    experiment_running = True
    threading.Thread(
        target=_run_startup,
        args=(req_body, extra_faust_urls, _experiment_gen),
        daemon=True,
    ).start()


def _schedule_queue_cleanup(item_id: str) -> None:
    def _remove():
        with _queue_lock:
            for i, item in enumerate(_experiment_queue):
                if item["id"] == item_id:
                    del _experiment_queue[i]
                    break
    threading.Timer(30, _remove).start()


def _find_result(item_id: str, repeat_index: int) -> str | None:
    """run_id of the result JSON which gets written for this queue item"""
    for f in sorted(glob_module.glob(os.path.join(METRICS_DIR, "*.json")), reverse=True):
        try:
            with open(f) as fh:
                cfg = json.load(fh).get("config", {})
            if cfg.get("queue_item_id") == item_id and cfg.get("repeat_index") == repeat_index:
                return os.path.splitext(os.path.basename(f))[0]
        except Exception:
            pass
    return None


def _run_queue_executor() -> None:
    global _queue_active
    _queue_stop.clear()
    while not _queue_stop.is_set():
        time.sleep(2)
        if experiment_running or _startup_in_progress:
            continue

        with _queue_lock:
            # Find first item still needing work
            item = next(
                (i for i in _experiment_queue if i["status"] in ("queued", "running") and i["repeat_done"] < i["repeats"]),
                None,
            )
            if item is None:
                if sa_tuner.search_active:
                    continue
                _queue_active = False
                return
            item["status"] = "running"

        if item["repeat_done"] > 0 and (item.get("save_checkpoint") or item.get("load_checkpoint_first")) \
                and not _checkpoint_exists(item["config"]["algorithm"]):
            with _queue_lock:
                item["status"] = "error"
                item["error"] = ("no checkpoint found for the warm-start repeat "
                                 "(repeat 1 saved nothing, or the learner is unreachable)")
            continue

        _queue_start_run(item)
        t_start = time.monotonic()
        with _queue_lock:
            item["_started_at"] = t_start

        run = _instr
        # Wait for this run to complete
        while experiment_running or _startup_in_progress:
            time.sleep(2)
        t_end = time.monotonic()

        repeat_index = item["repeat_done"] + 1
        run_id = None
        for _ in range(5):  # the JSON lands just before the sim process reports stopped
            run_id = _find_result(item["id"], repeat_index)
            if run_id:
                break
            time.sleep(1)

        with _instr_lock:
            run["run"] = {
                "result_run_id": run_id,
                "wall_total_s": round(t_end - t_start, 3),
                "startup_done_to_sim_stopped_s": (round(t_end - run["_startup_done"], 3)
                                                  if "_startup_done" in run else None),
                "sim_stopped_to_result_s": round(time.monotonic() - t_end, 3) if run_id else None,
            }
        _write_instrumentation(run)

        with _queue_lock:
            if run_id is None:
                item["status"] = "error"
                item["error"] = f"repeat {repeat_index} produced no result JSON (startup failed, killed or crashed)"
                continue
            item.pop("_started_at", None)
            item.setdefault("run_ids", []).append(run_id)
            item.setdefault("run_durations_s", []).append(round(time.monotonic() - t_start, 1))
            _group_durations.setdefault(item["group_id"], []).append(time.monotonic() - t_start)
            item["repeat_done"] += 1
            if item["repeat_done"] >= item["repeats"]:
                item["status"] = "done"
                _schedule_queue_cleanup(item["id"])
            else:
                item["status"] = "queued"

    _queue_active = False


def _consume_resource_metrics() -> None:
    """Daemon thread: reads from resource_metrics topic and appends JSON events to resource_events"""
    while True:
        try:
            from kafka import KafkaConsumer
            unique_group = f"dashboard_res_{os.getpid()}_{int(time.time())}"
            consumer = KafkaConsumer(
                TOPIC_RESOURCE_METRICS,
                bootstrap_servers=[KAFKA_BOOTSTRAP],
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
                auto_offset_reset="latest",
                group_id=unique_group,
            )
            for msg in consumer:
                with log_lock:
                    global resource_event_seq
                    resource_event_seq += 1
                    resource_events.append({"_seq": resource_event_seq, **msg.value})
        except Exception:
            pass  # Kafka not available yet, retry
        time.sleep(5)


threading.Thread(target=_consume_resource_metrics, daemon=True).start()


_docker_monitor = None


def _start_docker_monitor() -> None:
    global _docker_monitor
    while True:
        try:
            from common.monitoring.resource_monitor import DockerResourceMonitor
            m = DockerResourceMonitor(container="kafka")
            m.start()
            if m._thread is not None:
                m.paused = not _current_config.get("telemetry", True)
                _docker_monitor = m
                return
        except Exception:
            pass
        time.sleep(5)


threading.Thread(target=_start_docker_monitor, daemon=True).start()


def _run_startup(req_body: dict, extra_faust_urls: list[str], my_gen: int) -> None:
    global experiment_running, _startup_in_progress

    # Stop proxy threads from the previous experiment
    _stop_all_proxies()

    run = _instr
    log_proxies = req_body.get("log_proxies", req_body.get("telemetry", True))
    t_startup = time.monotonic()

    def _timed(step: str, fn):
        t = time.monotonic()
        ok = fn()
        with _instr_lock:
            run["startup"]["steps"].append({
                "step": step,
                "start_s": round(t - t_startup, 3),
                "duration_s": round(time.monotonic() - t, 3),
                "ok": bool(ok),
            })
        return ok

    def _stopped() -> bool:
        return _startup_stop.is_set()

    def _clear_agent_logs(url: str) -> None:
        """Ask a control agent to clear its log deque"""
        try:
            httpx.post(f"{url}/logs/clear", timeout=3, headers=_agent_headers())
        except Exception:
            pass

    def _prepare_proxy(agent_url: str, log_key: str, label: str = "") -> None:
        """Clear agent logs, then start a proxy"""
        if not log_proxies:
            with log_lock:
                _log_append(log_key, f"[dashboard] log proxies off: {label or log_key} logs are not proxied for this run")
            return
        _clear_agent_logs(agent_url)
        _start_one_proxy(agent_url, _deque_map[log_key], my_gen, label=label, log_key=log_key)

    def _post(url: str, log_key: str, label: str, timeout: float) -> bool:
        """POST to agent /start, append status message to deque"""
        if _stopped():
            return False
        try:
            r = httpx.post(url, json=req_body, timeout=timeout, headers=_agent_headers())
            if _stopped():
                return False
            data = r.json()
            msg = f"[dashboard] {label}: {data.get('status', '?')} {data.get('message', '')}".rstrip()
            with log_lock:
                _log_append(log_key, msg)
            return r.status_code < 400 and data.get("status") not in ("error",)
        except Exception as e:
            if _stopped():
                return False
            with log_lock:
                _log_append(log_key, f"[dashboard] ERROR reaching {label} agent: {e}")
            return False

    def _prep_and_post(agent_url: str, log_key: str, label: str, timeout: float, proxy_label: str = "") -> bool:
        _prepare_proxy(agent_url, log_key, label=proxy_label)
        return _post(f"{agent_url}/start", log_key, label, timeout)

    try:
        # Step 1: Kafka. clear -> proxy -> start
        if _stopped():
            return
        with log_lock:
            _log_append("kafka", "[dashboard] Starting Kafka...")
        if not _timed("kafka", lambda: _prep_and_post(AGENT_LAYER2_URL, "kafka", "Kafka", 120)):
            if not _stopped():
                with log_lock:
                    _log_append("kafka", "[dashboard] Kafka startup failed — aborting.")
                experiment_running = False
            return

        # Step 2: Stop extra Faust VMs before primary resets topics
        if extra_faust_urls:
            def _stop_extras():
                for url in extra_faust_urls:
                    threading.Thread(
                        target=lambda u=url: _try_stop_agent(u),
                        daemon=True,
                    ).start()
                time.sleep(2)
                return True
            _timed("stop_extra_actors", _stop_extras)

        # Step 2: Faust primary. clear -> proxy -> start (blocks until ready)
        if _stopped():
            return
        with log_lock:
            _log_append("faust", "[dashboard] Starting Faust workers...")
        if not _timed("faust_primary", lambda: _prep_and_post(AGENT_LAYER3_URL, "faust", "Faust", 300, "actors1")):
            if not _stopped():
                with log_lock:
                    _log_append("faust", "[dashboard] Faust startup failed — aborting.")
                experiment_running = False
            return

        if _stopped():
            return

        # Extra Faust VMs, start in parallel and wait for all before proceeding to Layer 4
        extra_req = {**req_body, "wait": True, "reset_topics": False}
        extra_results: list[bool] = [False] * len(extra_faust_urls)
        extra_lock = threading.Lock()

        def _start_extra(idx: int, url: str) -> None:
            label = f"actors{idx + 2}"
            _prepare_proxy(url, "faust", label=label)
            try:
                r = httpx.post(f"{url}/start", json=extra_req, timeout=300, headers=_agent_headers())
                ok = r.status_code < 400 and r.json().get("status") not in ("error",)
            except Exception as e:
                with log_lock:
                    _log_append("faust", f"[dashboard] ERROR reaching {label} agent: {e}")
                ok = False
            with extra_lock:
                extra_results[idx] = ok

        extra_threads = [
            threading.Thread(target=_start_extra, args=(i, url), daemon=True)
            for i, url in enumerate(extra_faust_urls)
        ]
        def _run_extras():
            for t in extra_threads:
                t.start()
            for t in extra_threads:
                t.join()
            return all(extra_results)
        if extra_threads:
            _timed("faust_extra", _run_extras)

        if _stopped():
            return

        failed = [f"actors{i + 2}" for i, ok in enumerate(extra_results) if not ok]
        if failed:
            with log_lock:
                _log_append("faust", f"[dashboard] WARNING: {', '.join(failed)} did not report ready — continuing anyway.")

        # Step 3: Learner. clear -> proxy -> start
        if _stopped():
            return
        with log_lock:
            _log_append("learner", "[dashboard] Starting learner...")
        if not _timed("learner", lambda: _prep_and_post(AGENT_LAYER4_URL, "learner", "Learner", 120)):
            if not _stopped():
                with log_lock:
                    _log_append("learner", "[dashboard] Learner startup failed — aborting.")
                experiment_running = False
            return

        # Step 4: Simulation. clear -> proxy -> start
        if _stopped():
            return
        with log_lock:
            _log_append("sim", "[dashboard] Starting simulation...")
        if _timed("simulation", lambda: _prep_and_post(AGENT_LAYER1_URL, "sim", "Simulation", 30)):
            with _instr_lock:
                run["startup"]["completed"] = True

    except Exception as e:
        if not _stopped():
            with log_lock:
                _log_append("sim", f"[dashboard] Unexpected error in startup: {e}")
            experiment_running = False
    finally:
        with _instr_lock:
            run["startup"]["total_s"] = round(time.monotonic() - t_startup, 3)
            run["_startup_done"] = time.monotonic()
        # Only clear the flag if experiment is active since a new experiment may have claimed ownership already
        if _experiment_gen == my_gen:
            _startup_in_progress = False


@app.route("/live")
def live_page():
    return render_template("experiment.html", page="live")


def _kill_running_experiment() -> None:
    """Abort any running startup/experiment and fire stop commands to all agents."""
    global experiment_running, _startup_in_progress

    _startup_stop.set()
    _startup_in_progress = False
    experiment_running = False

    _stop_all_proxies()

    all_stop_urls = [AGENT_LAYER1_URL, AGENT_LAYER3_URL, AGENT_LAYER4_URL]
    all_stop_urls += [u.strip() for u in AGENT_LAYER3_EXTRA_URLS.split(",") if u.strip()]
    run = _instr
    t_kill = time.monotonic()
    per_agent: dict[str, float] = {}

    def _timed_stop(u: str) -> None:
        _try_stop_agent(u)
        per_agent[u] = round(time.monotonic() - t_kill, 3)

    stop_threads = [threading.Thread(target=_timed_stop, args=(url,), daemon=True) for url in all_stop_urls]
    for t in stop_threads:
        t.start()

    def _record_stop() -> None:
        for t in stop_threads:
            t.join()
        with _instr_lock:
            run["stop"] = {"trigger": "kill", "per_agent_s": per_agent,
                           "total_s": round(time.monotonic() - t_kill, 3)}
        _write_instrumentation(run)

    threading.Thread(target=_record_stop, daemon=True).start()

    with log_lock:
        sim_log_lines.clear()
        kafka_log_lines.clear()
        faust_log_lines.clear()
        learner_log_lines.clear()


@app.route("/stop", methods=["POST"])
def stop():
    sa_tuner.cancel()
    _kill_running_experiment()
    return jsonify({"status": "stopped"})


def _try_stop_agent(url: str) -> None:
    try:
        httpx.post(f"{url}/stop", timeout=10, headers=_agent_headers())
    except Exception:
        pass


@app.route("/stream/all")
def stream_all():
    gen = _make_multiplexed_sse_generator(_experiment_gen)
    return Response(stream_with_context(gen()), mimetype="text/event-stream")


@app.route("/api/instrumentation")
def api_instrumentation():
    return jsonify(_instr_snapshot(_instr))


@app.route("/api/instrumentation/<name>")
def api_instrumentation_file(name):
    if not re.fullmatch(r"[\w-]+_r\d+", name):
        return jsonify({"error": "invalid name"}), 400
    path = os.path.join(INSTRUMENTATION_DIR, f"{name}.json")
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    with open(path) as f:
        return jsonify(json.load(f))


@app.route("/status")
def status():
    # Return cached statuses instantly
    return jsonify({
        "sim": _cached_agent_statuses["sim"],
        "faust": _cached_agent_statuses["faust"],
        "learner": _cached_agent_statuses["learner"],
        "experiment_running": experiment_running,
    })


@app.route("/kafka-status")
def kafka_status():
    # Return cached output instantly
    return jsonify({
        "output": _cached_agent_statuses["kafka"],
        "stable": _cached_agent_statuses["kafka_stable"]
    })


@app.route("/analyze")
def analyze():
    """Analysis page"""
    return render_template("analyze.html", page="analyze")


@app.route("/")
def index():
    return render_template("control.html", page="control")


@app.route("/api/quick-status")
def api_quick_status():
    """Returns local state only — no outbound agent calls, responds instantly."""
    return jsonify({
        "experiment_running": experiment_running,
        "config": _current_config,
    })


@app.route("/api/networks")
def api_networks():
    """List available networks with metadata. Each item: {name, duration, period}"""
    return jsonify(list_available_networks_with_meta())


@app.route("/api/checkpoint-status")
def api_checkpoint_status():
    algorithm = request.args.get("algorithm", "")
    p = _CHECKPOINT_PATHS.get(algorithm)
    if p is None:
        return jsonify({"exists": False, "filename": None})

    # The checkpoint lives on the LEARNER VM's local disk, so request it
    if AGENT_LAYER4_URL:
        try:
            with httpx.Client(timeout=5, headers=_agent_headers()) as client:
                r = client.get(f"{AGENT_LAYER4_URL}/checkpoint-status", params={"algorithm": algorithm})
                if r.status_code == 200:
                    return jsonify(r.json())
        except Exception:
            pass
        return jsonify({"exists": False, "unknown": True, "filename": p.name})

    return jsonify({"exists": p.exists(), "filename": p.name})


def _checkpoint_status(algorithm: str) -> dict:
    """{exists, train_steps} from the learner agent"""
    p = _CHECKPOINT_PATHS.get(algorithm)
    if p is None:
        return {"exists": False, "train_steps": None}
    if AGENT_LAYER4_URL:
        try:
            with httpx.Client(timeout=5, headers=_agent_headers()) as client:
                r = client.get(f"{AGENT_LAYER4_URL}/checkpoint-status", params={"algorithm": algorithm})
                if r.status_code == 200:
                    d = r.json()
                    return {"exists": bool(d.get("exists")), "train_steps": d.get("train_steps")}
        except Exception:
            pass
        return {"exists": None, "train_steps": None}
    return {"exists": p.exists(), "train_steps": None}


def _checkpoint_exists(algorithm: str) -> bool:
    return bool(_checkpoint_status(algorithm).get("exists"))


def _delete_checkpoint(algorithm: str) -> bool:
    p = _CHECKPOINT_PATHS.get(algorithm)
    if p is None:
        return False
    if AGENT_LAYER4_URL:
        with httpx.Client(timeout=5, headers=_agent_headers()) as client:
            r = client.delete(f"{AGENT_LAYER4_URL}/checkpoint", params={"algorithm": algorithm})
            r.raise_for_status()
            return bool(r.json().get("deleted"))
    if p.exists():
        p.unlink()
        return True
    return False


@app.route("/api/checkpoint", methods=["DELETE"])
def api_checkpoint_delete():
    algorithm = request.args.get("algorithm", "")
    if algorithm not in _CHECKPOINT_PATHS:
        return jsonify({"error": "unknown algorithm"}), 400
    try:
        return jsonify({"deleted": _delete_checkpoint(algorithm)})
    except httpx.HTTPStatusError as e:
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = str(e)
        return jsonify({"error": detail}), e.response.status_code
    except Exception as e:
        return jsonify({"error": f"could not reach the learner: {e}"}), 502


_meta_cache: dict = {}  # run_id -> list summary of the result JSON

_META_CONFIG_KEYS = ("algorithm", "network_name", "bottleneck", "bottleneck_edges", "jam_requested", "jam_factor",
                     "queue_item_id", "repeat_index", "repeats", "warm_start", "loaded_checkpoint", "timed_out")


def _get_meta(path: str, run_id: str) -> dict:
    if run_id not in _meta_cache:
        try:
            with open(path) as f:
                data = json.load(f)
            cfg = data.get("config", {})
            att = data.get("att") or {}
            _meta_cache[run_id] = {
                "group_id": data.get("group_id", ""),
                "att_mean": att.get("mean"),
                **{k: cfg.get(k) for k in _META_CONFIG_KEYS},
            }
        except Exception:
            return {"network_name": "unknown", "group_id": ""}
    return _meta_cache[run_id]


@app.route("/api/experiments")
def api_experiments():
    """List all experiment JSON files, grouped"""
    files = glob_module.glob(os.path.join(METRICS_DIR, "*.json"))
    result = []
    for f in sorted(files, reverse=True):
        name = os.path.splitext(os.path.basename(f))[0]
        parts = name.rsplit("_", 2)
        if len(parts) == 3:
            _, date_str, time_str = parts
            date_fmt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            time_fmt = f"{time_str[:2]}:{time_str[2:4]}:{time_str[4:]}"
        else:
            date_fmt, time_fmt = "unknown", ""
        meta = _get_meta(f, name)
        result.append({"run_id": name, "date": date_fmt, "time": time_fmt, **meta})
    return jsonify(result)


@app.route("/api/experiments", methods=["DELETE"])
def api_experiments_delete_all():
    """Delete all experiment JSON files"""
    files = glob_module.glob(os.path.join(METRICS_DIR, "*.json"))
    for f in files:
        try:
            os.remove(f)
        except Exception:
            pass
    _meta_cache.clear()
    return jsonify({"ok": True, "count": len(files)})


@app.route("/api/experiment/<run_id>")
def api_experiment(run_id):
    """Return full JSON for an experiment run"""
    if not re.fullmatch(r"[a-zA-Z0-9_]+", run_id):
        return jsonify({"error": "invalid run_id"}), 400
    path = os.path.join(METRICS_DIR, run_id + ".json")
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    with open(path) as f:
        return jsonify(json.load(f))


_geometry_cache: dict = {}  # network name -> geometry dict


@app.route("/api/network/<name>/geometry")
def api_network_geometry(name):
    """Edge polylines for a network, used by the route/loop map in Analyze"""
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", name):
        return jsonify({"error": "invalid network name"}), 400
    if name not in _geometry_cache:
        net_path = network_file_path(name)
        if not os.path.isfile(net_path):
            return jsonify({"error": "network not found"}), 404
        try:
            _geometry_cache[name] = load_edge_geometry(net_path)
        except Exception as e:
            return jsonify({"error": f"failed to parse network: {e}"}), 500
    return jsonify(_geometry_cache[name])


@app.route("/api/trajectories/<run_id>")
def api_trajectories(run_id):
    """Per-vehicle edge paths captured for a run"""
    if not re.fullmatch(r"[a-zA-Z0-9_]+", run_id):
        return jsonify({"error": "invalid run_id"}), 400
    path = os.path.join(TRAJECTORIES_DIR, run_id + ".json")
    if not os.path.isfile(path):
        return jsonify({"available": False}), 404
    with open(path) as f:
        data = json.load(f)
    data["available"] = True
    return jsonify(data)


@app.route("/api/experiment/<run_id>", methods=["DELETE"])
def api_experiment_delete(run_id):
    """Delete an experiment JSON file"""
    if not re.fullmatch(r"[a-zA-Z0-9_]+", run_id):
        return jsonify({"error": "invalid run_id"}), 400
    path = os.path.join(METRICS_DIR, run_id + ".json")
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    os.remove(path)
    traj_path = os.path.join(TRAJECTORIES_DIR, run_id + ".json")
    if os.path.isfile(traj_path):
        try:
            os.remove(traj_path)
        except Exception:
            pass
    _meta_cache.pop(run_id, None)  # evict cached meta
    return jsonify({"ok": True})


@app.route("/api/queue", methods=["GET"])
def api_queue_get():
    with _queue_lock:
        return jsonify(list(_experiment_queue))


@app.route("/api/queue", methods=["POST"])
def api_queue_add():
    body = request.get_json(force=True) or {}
    repeats = max(1, int(body.pop("repeats", 1)))
    load_checkpoint = bool(body.pop("load_checkpoint", False))
    save_checkpoint = bool(body.pop("save_checkpoint", False))
    reset_checkpoint_first = bool(body.pop("reset_checkpoint_first", False))
    # Allow caller to supply custom group_id, otherwise auto generate
    group_id = body.pop("group_id", None) or f"seq_{_dt.now().strftime('%Y%m%d_%H%M%S')}_{_uuid_mod.uuid4().hex[:4]}"
    cfg = {**body, "restart_faust": True}
    try:
        req = StartRequest(**cfg)
    except ValidationError as e:
        return jsonify({"error": "; ".join(err["msg"] for err in e.errors())}), 400
    algo = algorithms.get(req.algorithm)
    cfg["hyperparams"] = req.hyperparams
    cfg["speed_ema_alpha"] = req.speed_ema_alpha
    if not algo.checkpoint:
        load_checkpoint = save_checkpoint = reset_checkpoint_first = False
    if not algo.bottleneck:
        cfg["jam_top_edges"] = 0
    item = {
        "id": str(_uuid_mod.uuid4()),
        "config": cfg,
        "repeats": repeats,
        "repeat_done": 0,
        "load_checkpoint_first": load_checkpoint,
        "save_checkpoint": save_checkpoint,
        "reset_checkpoint_first": reset_checkpoint_first,
        "status": "queued",
        "group_id": group_id,
    }
    with _queue_lock:
        _experiment_queue.append(item)
    return jsonify(item), 201


@app.route("/api/jam-preview")
def api_jam_preview():
    """Which edges a bottleneck of size n would slow"""
    from layer1_simulation.bottleneck import edge_usage
    network = request.args.get("network", "")
    if not re.fullmatch(r"[a-zA-Z0-9_\-]+", network or ""):
        return jsonify({"error": "invalid network name"}), 400
    try:
        n = max(1, int(request.args.get("n", "4")))
    except ValueError:
        return jsonify({"error": "n must be a number"}), 400
    route_file = route_file_path(network)
    if not os.path.exists(route_file):
        return jsonify({"error": "no route file for this network"}), 404
    usage = edge_usage(route_file)
    total = sum(usage.values())
    if not total:
        return jsonify({"error": "no vehicle routes in route file"}), 404
    edges = [e for e, _ in usage.most_common(n)]
    return jsonify({
        "edges": edges,
        "share": round(sum(usage[e] for e in edges) / total, 4),
        "usage": dict(usage),
    })


@app.route("/api/algorithms")
def api_algorithms():
    return jsonify(algorithms.descriptors())


@app.route("/api/groups")
def api_groups():
    """Return all experiment groups"""
    files = glob_module.glob(os.path.join(METRICS_DIR, "*.json"))
    groups: dict = {}  # group_id -> {count, network_name, algorithm, date}
    for f in sorted(files):
        run_id = os.path.splitext(os.path.basename(f))[0]
        meta = _get_meta(f, run_id)
        gid = meta.get("group_id", "")
        if not gid:
            continue
        if gid not in groups:
            try:
                parts = run_id.rsplit("_", 2)
                date_fmt = f"{parts[1][:4]}-{parts[1][4:6]}-{parts[1][6:]}" if len(parts) == 3 else "unknown"
                groups[gid] = {
                    "group_id": gid,
                    "label": gid,
                    "count": 0,
                    "network_name": meta["network_name"],
                    "algorithm": meta["algorithm"],
                    "date": date_fmt,
                    **{k: meta.get(k) for k in ("bottleneck", "bottleneck_edges", "jam_requested", "jam_factor")},
                }
            except Exception:
                groups[gid] = {"group_id": gid, "label": gid, "count": 0, "network_name": "", "algorithm": "", "date": ""}
        groups[gid]["count"] += 1

    result = sorted(
        [g for g in groups.values() if not g["group_id"].startswith("seq_") and g["count"] > 0],
        key=lambda g: g.get("date", ""),
        reverse=True,
    )
    return jsonify(result)


@app.route("/api/group/<group_id>")
def api_group(group_id):
    """Return all experiments in a group with summary data"""
    if not re.fullmatch(r"[a-zA-Z0-9_]+", group_id):
        return jsonify({"error": "invalid group_id"}), 400

    meta = {"group_id": group_id, "label": group_id}

    files = glob_module.glob(os.path.join(METRICS_DIR, "*.json"))
    experiments = []
    for f in sorted(files):
        run_id = os.path.splitext(os.path.basename(f))[0]
        m = _get_meta(f, run_id)
        if m.get("group_id") != group_id:
            continue
        try:
            with open(f) as fh:
                d = json.load(fh)
            experiments.append({
                "run_id": run_id,
                "status": "done",
                "timed_out": d.get("config", {}).get("timed_out", False),
                "timed_out_at_s": d.get("config", {}).get("timed_out_at_s"),
                "config": d.get("config", {}),
                "att": d.get("att", {}),
                "routing": d.get("routing", {}),
                "throughput": d.get("throughput_vehicles_per_sim_min"),
                "congestion": d.get("congestion_vehicle_seconds"),
                "sim_steps": d.get("sim_steps"),
                "wall_duration_s": d.get("wall_duration_s"),
                "loops": d.get("loops"),
                "departures": d.get("departures"),
                "sa": d.get("sa"),
            })
        except Exception:
            pass

    sa_summary = None
    if group_id.startswith("sa_"):
        by_iter = {}
        confirm = None
        for e in experiments:
            sa = e.get("sa")
            if not sa or sa.get("iteration") is None:
                continue
            if sa.get("confirm"):
                confirm = sa
                continue
            by_iter.setdefault(sa["iteration"], sa)
        iterations = [by_iter[k] for k in sorted(by_iter)]
        if iterations:
            valid = [it for it in iterations if it.get("att") is not None]
            best = min(valid, key=lambda it: it["att"]) if valid else None
            ref = iterations[-1]
            sa_summary = {
                "iterations": iterations,
                "best": best,
                "confirm": confirm,
                "max_iteration": max(by_iter),
                "t_init": ref.get("t_init"),
                "alpha": ref.get("alpha"),
                "n_iter": ref.get("n_iter"),
                "settings": {k: ref.get(k) for k in ("repeats", "warm_start", "score_mode")},
            }

    with _queue_lock:
        for item in _experiment_queue:
            if item.get("group_id") != group_id:
                continue
            if item["status"] == "done":
                continue
            done_here = sum(1 for e in experiments if e["config"].get("queue_item_id") == item["id"])
            if done_here >= item["repeats"]:
                continue
            experiments.append({
                "run_id": item["id"],
                "status": item["status"],
                "error": item.get("error"),
                "timed_out": False,
                "timed_out_at_s": None,
                "config": {**item.get("config", {}), "queue_item_id": item["id"],
                           "repeat_index": item["repeat_done"] + 1, "repeats": item["repeats"]},
                "att": None,
                "routing": None,
                "throughput": None,
                "congestion": None,
                "sim_steps": None,
            })

    return jsonify({"meta": meta, "experiments": experiments, "sa_summary": sa_summary})


@app.route("/api/queue/<item_id>", methods=["DELETE"])
def api_queue_delete(item_id):
    with _queue_lock:
        for i, item in enumerate(_experiment_queue):
            if item["id"] == item_id:
                if item["status"] == "running":
                    return jsonify({"error": "Cannot remove a running item"}), 409
                _experiment_queue.pop(i)
                return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404


@app.route("/api/group/<group_id>", methods=["DELETE"])
def api_group_delete(group_id):
    """Delete all experiments in a group"""
    if not re.fullmatch(r"[a-zA-Z0-9_]+", group_id):
        return jsonify({"error": "invalid group_id"}), 400

    files = glob_module.glob(os.path.join(METRICS_DIR, "*.json"))
    deleted = 0
    for f in files:
        try:
            with open(f) as fh:
                d = json.load(fh)
            if d.get("group_id") == group_id:
                os.remove(f)
                _meta_cache.pop(os.path.splitext(os.path.basename(f))[0], None)
                deleted += 1
        except Exception:
            pass

    with _queue_lock:
        _experiment_queue[:] = [i for i in _experiment_queue if i.get("group_id") != group_id]

    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/queue/start", methods=["POST"])
def api_queue_start():
    global _queue_active
    with _queue_lock:
        if _queue_active:
            return jsonify({"status": "already_running"})
        has_pending = any(i["status"] in ("queued", "running") and i["repeat_done"] < i["repeats"] for i in _experiment_queue)
        if not has_pending:
            return jsonify({"status": "empty"})
        _queue_active = True
    _queue_stop.clear()
    threading.Thread(target=_run_queue_executor, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/queue/stop", methods=["POST"])
def api_queue_stop():
    global _queue_active
    _queue_stop.set()
    _queue_active = False
    sa_tuner.cancel()
    return jsonify({"status": "stopped"})


@app.route("/api/queue/clear", methods=["POST"])
def api_queue_clear():
    with _queue_lock:
        before = len(_experiment_queue)
        _experiment_queue[:] = [i for i in _experiment_queue if i["status"] == "running" or i["status"] == "done"]
    return jsonify({"removed": before - len(_experiment_queue)})


@app.route("/api/queue/status")
def api_queue_status():
    with _queue_lock:
        total = len(_experiment_queue)
        done = sum(1 for i in _experiment_queue if i["status"] == "done")
        running = sum(1 for i in _experiment_queue if i["status"] == "running")
        queued = sum(1 for i in _experiment_queue if i["status"] == "queued")
    return jsonify({"active": _queue_active, "total": total, "done": done, "running": running, "queued": queued})


# ── Simulated Annealing tuner ──────────────────────────────────────────────
sa_tuner = SimulatedAnnealingTuner(
    metrics_dir=METRICS_DIR,
    state_dir=STATE_DIR,
    experiment_queue=_experiment_queue,
    queue_lock=_queue_lock,
    kill_running_experiment=_kill_running_experiment,
)


@app.route("/api/sa/start", methods=["POST"])
def api_sa_start():
    cfg = request.get_json(force=True) or {}
    problem = sa_tuner.validate(cfg)
    if problem:
        return jsonify({"error": problem}), 400
    if not sa_tuner.start(cfg):
        return jsonify({"error": "SA search already running"}), 409
    return jsonify({"ok": True})


@app.route("/api/sa/discard", methods=["POST"])
def api_sa_discard():
    body = request.get_json(silent=True) or {}
    killed = sa_tuner.discard(body.get("group_id"))
    return jsonify({"ok": True, "killed": killed})


@app.route("/api/sa/status")
def api_sa_status_route():
    st = sa_tuner.status()
    gid = st.get("group_id")
    durations = _group_durations.get(gid, []) if gid else []
    with _queue_lock:
        cur = next((i for i in _experiment_queue if i.get("group_id") == gid and i["status"] == "running"), None)
        cur_elapsed = time.monotonic() - cur["_started_at"] if cur and cur.get("_started_at") else 0.0
    runs_done = len(durations)
    st["runs_done"] = runs_done
    st["runs_remaining"] = max(0, st["runs_total"] - runs_done) if st.get("runs_total") else None
    st["avg_run_s"] = round(sum(durations) / runs_done, 1) if runs_done else None
    if st["runs_remaining"] is not None and st["avg_run_s"]:
        st["eta_s"] = round(max(0.0, st["runs_remaining"] * st["avg_run_s"] - cur_elapsed))
    else:
        st["eta_s"] = None
    return jsonify(st)


@app.route("/api/sa/has-state-file")
def api_sa_has_state_file():
    return jsonify({"exists": sa_tuner.has_state_file()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=False, threaded=True, port=5000)
