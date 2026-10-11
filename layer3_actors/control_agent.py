"""
Layer 3
Faust actors control agent (port 8003)

Usage:
  uvicorn layer3_actors.control_agent:app --host 0.0.0.0 --port 8003
"""
import os
import sys
import time

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.config import KAFKA_BOOTSTRAP, FAUST_APP_ID, AGENT_LAYER2_URL, AGENT_API_KEY, LEARNER_GROUP_ID
from common.controls.control_base import ProcessManager, StartRequest, LayerStatus, StopResponse, make_sse_generator, make_api_key_dep, algorithm_env

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = FastAPI(dependencies=[make_api_key_dep(AGENT_API_KEY)])
mgr = ProcessManager(maxlen=500)

def _agent_headers() -> dict:
    return {"X-API-Key": AGENT_API_KEY} if AGENT_API_KEY else {}

def _get_consumer_group_state(layer2_url: str) -> dict:
    """Fetch consumer group state from the layer2 agent (which has docker access)."""
    r = httpx.get(f"{layer2_url}/consumer-group-state", timeout=15, headers=_agent_headers())
    return r.json()

def _try_delete_consumer_groups() -> None:
    """Attempt to delete the Faust consumer group and related groups via AdminClient."""
    try:
        from kafka.admin import KafkaAdminClient
        admin = KafkaAdminClient(bootstrap_servers=[KAFKA_BOOTSTRAP], request_timeout_ms=5000)
        try:
            groups_to_delete = [FAUST_APP_ID]
            # Also delete per-topic consumer groups (e.g. model_weights_worker, the learner's)
            all_groups = admin.list_consumer_groups()
            for g, _ in all_groups:
                if g.startswith(FAUST_APP_ID) or g in ("model_weights_worker", LEARNER_GROUP_ID):
                    if g not in groups_to_delete:
                        groups_to_delete.append(g)
            admin.delete_consumer_groups(groups_to_delete)
        finally:
            admin.close()
    except Exception:
        pass  # Best-effort — fall through to polling


def _wait_for_consumers_gone(layer2_url: str, timeout: int = 15) -> None:
    """Try to delete consumer groups, then poll until Empty/Dead or absent."""
    _try_delete_consumer_groups()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = _get_consumer_group_state(layer2_url)
            if data.get("state") in ("Empty", "Dead", "unknown", "error"):
                return
        except Exception:
            return
        time.sleep(1)


def _wait_for_workers_ok(mgr, num_workers: int, timeout: int = 120) -> bool:
    """
    Wait until all Faust workers have printed their startup 'OK ^' message
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with mgr.lock:
            lines = list(mgr.log_deque)
        ok_count = sum(1 for line in lines if "OK ^" in line)
        if ok_count >= num_workers:
            return True
        if not mgr.is_running():
            return False
        time.sleep(1)
    return False


@app.post("/start")
def start(req: StartRequest) -> LayerStatus:
    mgr.stop()

    bootstrap = req.kafka_bootstrap or KAFKA_BOOTSTRAP
    num_workers = req.num_workers

    env = os.environ.copy()
    env.update({
        **algorithm_env(req),
        "NUM_WORKERS": str(num_workers),
        "NUM_ACTOR_VMS": str(req.num_actor_vms),
        "FAUST_SKIP_RESET": "0" if req.reset_topics else "1",
        "KAFKA_BOOTSTRAP_SERVERS": bootstrap,
        "NETWORK_NAME": req.network_name,
        "TELEMETRY": "true" if req.telemetry else "false",
        "PYTHONUNBUFFERED": "1",
    })
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = PROJECT_ROOT + (os.pathsep + existing_pp if existing_pp else "")

    layer2_url = AGENT_LAYER2_URL

    if req.restart_faust:
        mgr.append_log("[agent] Waiting for old workers to disconnect...")
        _wait_for_consumers_gone(layer2_url)

    mgr.append_log("[agent] Starting Faust workers (reset + create topics)...")
    mgr.start_proc([sys.executable, "startup_scripts/start_faust.py"], env=env, cwd=PROJECT_ROOT)

    if not req.wait:
        return LayerStatus(
            layer="layer3", status="starting",
            pid=mgr.proc.pid if mgr.proc else None,
        )

    mgr.append_log("[agent] Waiting for Faust topics to be ready (up to 60s)...")
    topics_ready = mgr.wait_for_sentinel("[Faust] Topics ready", timeout=60)
    if topics_ready:
        mgr.append_log("[agent] Topics ready.")
    else:
        mgr.append_log("[agent] WARNING: Topics ready signal not seen after 60s, proceeding.")

    mgr.append_log(f"[agent] Waiting for {num_workers} Faust worker(s) to report OK (up to 2min)...")
    ready = _wait_for_workers_ok(mgr, num_workers, timeout=120)
    if ready:
        mgr.append_log("[agent] All Faust workers ready.")
        return LayerStatus(
            layer="layer3", status="running", message="ready",
            pid=mgr.proc.pid if mgr.proc else None,
        )
    else:
        mgr.append_log("[agent] ERROR: Workers did not report OK within 2min.")
        mgr.stop()
        return LayerStatus(layer="layer3", status="error", message="timeout waiting for worker OK")


@app.post("/stop")
def stop() -> StopResponse:
    mgr.stop()
    mgr.append_log("[agent] Faust workers stopped.")
    return StopResponse(status="stopped")


@app.get("/status")
def status() -> LayerStatus:
    if mgr.is_running():
        return LayerStatus(layer="layer3", status="running", pid=mgr.proc.pid)
    return LayerStatus(layer="layer3", status="stopped")


@app.post("/logs/clear")
def logs_clear():
    with mgr.lock:
        mgr.log_deque.clear()
    return {"ok": True}


@app.get("/logs/stream")
def logs_stream():
    gen = make_sse_generator(mgr)
    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
