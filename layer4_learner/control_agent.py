"""
Layer 4
Learner control agent (port 8004)

Usage:
  uvicorn layer4_learner.control_agent:app --host 0.0.0.0 --port 8004
"""
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import algorithms
from common.config import KAFKA_BOOTSTRAP, AGENT_API_KEY
from common.controls.control_base import ProcessManager, StartRequest, LayerStatus, StopResponse, make_sse_generator, make_api_key_dep, algorithm_env

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = FastAPI(dependencies=[make_api_key_dep(AGENT_API_KEY)])
mgr = ProcessManager(maxlen=500)


def _checkpoint(algorithm: str) -> tuple[algorithms.Algorithm, Path]:
    try:
        algo = algorithms.get(algorithm)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not algo.checkpoint:
        raise HTTPException(status_code=400, detail=f"{algorithm} has no checkpoint")
    return algo, Path(algorithms.checkpoint_path(algorithm))


@app.post("/start")
def start(req: StartRequest) -> LayerStatus:
    mgr.stop()

    ck = algorithms.checkpoint_path(req.algorithm)
    if req.reset_checkpoint and ck:
        ck = Path(ck)
        if ck.exists():
            ck.unlink()
            mgr.append_log(f"[agent] Wiped checkpoint {ck.name} before start")
        else:
            mgr.append_log(f"[agent] Checkpoint {ck.name} already absent")

    env = os.environ.copy()
    env.update({
        **algorithm_env(req),
        "KAFKA_BOOTSTRAP_SERVERS": req.kafka_bootstrap or KAFKA_BOOTSTRAP,
        "NETWORK_NAME": req.network_name,
        "LOAD_CHECKPOINT": "true" if req.load_checkpoint else "false",
        "SAVE_MODEL": "true" if req.save_model else "false",
        **({"CHECKPOINT_PATH": str(ck)} if ck else {}),
        "TELEMETRY": "true" if req.telemetry else "false",
        "PYTHONUNBUFFERED": "1",
    })

    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = PROJECT_ROOT + (os.pathsep + existing_pp if existing_pp else "")

    mgr.append_log("[agent] Starting learner...")
    mgr.start_proc([sys.executable, "startup_scripts/start_learner.py"], env=env, cwd=PROJECT_ROOT)

    if not req.wait:
        return LayerStatus(
            layer="layer4", status="starting",
            pid=mgr.proc.pid if mgr.proc else None,
        )

    mgr.append_log("[agent] Waiting for learner ready signal (up to 120s)...")
    ready = mgr.wait_for_sentinel("[Learner] Ready", timeout=120)
    if ready:
        mgr.append_log("[agent] Learner ready.")
        return LayerStatus(
            layer="layer4", status="running", message="ready",
            pid=mgr.proc.pid if mgr.proc else None,
        )
    else:
        mgr.append_log("[agent] ERROR: Learner not ready after 30s. Stopping learner.")
        mgr.stop()
        return LayerStatus(layer="layer4", status="error", message="timeout waiting for Ready")


@app.post("/stop")
def stop() -> StopResponse:
    mgr.stop()
    mgr.append_log("[agent] Learner stopped.")
    return StopResponse(status="stopped")


@app.get("/status")
def status() -> LayerStatus:
    if mgr.is_running():
        return LayerStatus(layer="layer4", status="running", pid=mgr.proc.pid)
    return LayerStatus(layer="layer4", status="stopped")


@app.get("/algorithms")
def list_algorithms():
    return algorithms.descriptors()


@app.get("/checkpoint-status")
def checkpoint_status(algorithm: str):
    """Report whether the checkpoint file exists on THIS (learner) VM's local disk."""
    algo, p = _checkpoint(algorithm)
    if not p.exists():
        return {"exists": False, "filename": p.name}
    info = {"exists": True, "filename": p.name, "mtime": p.stat().st_mtime}
    if algo.checkpoint_info:
        try:
            info.update(algorithms.import_string(algo.checkpoint_info)(p))
        except Exception as e:
            info["read_error"] = str(e)
    return info


@app.delete("/checkpoint")
def checkpoint_delete(algorithm: str):
    """Delete the checkpoint so the next run starts from random set of weights"""
    _, p = _checkpoint(algorithm)
    if mgr.is_running():
        raise HTTPException(status_code=409, detail="learner is running — stop the experiment first")
    existed = p.exists()
    if existed:
        p.unlink()
        mgr.append_log(f"[agent] Deleted checkpoint {p.name}")
    return {"deleted": existed, "filename": p.name}


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
    uvicorn.run(app, host="0.0.0.0", port=8004)
