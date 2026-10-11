"""
Layer 2
Kafka control agent (port 8002)

Usage:
  uvicorn layer2_message_bus.control_agent:app --host 0.0.0.0 --port 8002
"""
import os
import re
import subprocess
import sys
import time

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.config import KAFKA_BOOTSTRAP, FAUST_APP_ID, AGENT_API_KEY
from common.controls.control_base import ProcessManager, StartRequest, LayerStatus, StopResponse, make_sse_generator, make_api_key_dep

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE_DIR = os.path.join(PROJECT_ROOT, "layer2_message_bus")

app = FastAPI(dependencies=[make_api_key_dep(AGENT_API_KEY)])
mgr = ProcessManager(maxlen=500)

def _is_kafka_reachable(bootstrap: str = KAFKA_BOOTSTRAP) -> bool:
    try:
        from kafka.admin import KafkaAdminClient
        admin = KafkaAdminClient(bootstrap_servers=[bootstrap], request_timeout_ms=3000)
        admin.close()
        return True
    except Exception:
        return False


@app.post("/start")
def start(req: StartRequest) -> LayerStatus:
    with mgr.lock:
        mgr.log_deque.clear()
    bootstrap = req.kafka_bootstrap or KAFKA_BOOTSTRAP
    if _is_kafka_reachable(bootstrap):
        mgr.append_log("[agent] Kafka already running.")
        return LayerStatus(layer="layer2", status="running", message="already running")

    mgr.append_log("[agent] Starting Kafka (docker compose up -d)...")
    result = subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=COMPOSE_DIR, capture_output=True, text=True,
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        mgr.append_log(f"[agent] docker compose up failed: {msg}")
        mgr.append_log("[agent] Recovering: docker compose down --remove-orphans, then retry...")
        subprocess.run(
            ["docker", "compose", "down", "--remove-orphans"],
            cwd=COMPOSE_DIR, capture_output=True, text=True,
        )
        result = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=COMPOSE_DIR, capture_output=True, text=True,
        )
        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip()
            mgr.append_log(f"[agent] ERROR: docker compose failed after recovery: {msg}")
            return LayerStatus(layer="layer2", status="error", message=msg)
        mgr.append_log("[agent] Recovery succeeded.")

    for attempt in range(1, 21):
        if _is_kafka_reachable(bootstrap):
            mgr.append_log("[agent] Kafka ready.")
            return LayerStatus(layer="layer2", status="running", message="ready")
        mgr.append_log(f"[agent] Waiting for Kafka... (attempt {attempt}/20)")
        time.sleep(3)

    mgr.append_log("[agent] ERROR: Kafka did not become ready after 60s.")
    return LayerStatus(layer="layer2", status="error", message="timed out waiting for Kafka")


@app.get("/status")
def status() -> LayerStatus:
    if _is_kafka_reachable():
        return LayerStatus(layer="layer2", status="running")
    return LayerStatus(layer="layer2", status="stopped")


@app.post("/logs/clear")
def logs_clear():
    with mgr.lock:
        mgr.log_deque.clear()
    return {"ok": True}


@app.get("/logs/stream")
def logs_stream():
    gen = make_sse_generator(mgr)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/kafka-consumer-group")
def kafka_consumer_group():
    """Describe the Faust consumer group — used by the dashboard's Kafka status card."""
    try:
        result = subprocess.run(
            ["docker", "exec", "kafka", "kafka-consumer-groups",
             "--bootstrap-server", KAFKA_BOOTSTRAP,
             "--describe", "--group", FAUST_APP_ID],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout or result.stderr or "(no output)"
    except Exception as e:
        output = f"Error: {e}"
    return JSONResponse({"output": output})


@app.get("/consumer-group-state")
def consumer_group_state(group: str = FAUST_APP_ID):
    """
    Return the state and member count of the Faust consumer group
    Used by the layer3 agent to poll for complete Faust worker readiness
    """
    try:
        result = subprocess.run(
            ["docker", "exec", "kafka", "kafka-consumer-groups",
             "--bootstrap-server", KAFKA_BOOTSTRAP,
             "--describe", "--group", group, "--state"],
            capture_output=True, text=True, timeout=10,
        )
        m = re.search(r"(Stable|Empty|Dead|PreparingRebalance|CompletingRebalance)\s+(\d+)", result.stdout)
        if not m:
            return JSONResponse({"state": "unknown", "member_count": 0})

        return JSONResponse({"state": m.group(1), "member_count": int(m.group(2))})

    except Exception as e:
        return JSONResponse({"state": "error", "member_count": 0, "detail": str(e)})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
