"""
Layer 1
Simulation control agent (port 8001)

Usage:
  uvicorn layer1_simulation.control_agent:app --host 0.0.0.0 --port 8001
"""
import os
import sys

import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.config import KAFKA_BOOTSTRAP, AGENT_API_KEY
from common.controls.control_base import ProcessManager, StartRequest, LayerStatus, StopResponse, make_sse_generator, make_api_key_dep, algorithm_env

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = FastAPI(dependencies=[make_api_key_dep(AGENT_API_KEY)])
mgr = ProcessManager(maxlen=1000)

@app.post("/start")
def start(req: StartRequest) -> LayerStatus:
    mgr.stop()

    env = os.environ.copy()
    env.update({
        **algorithm_env(req),
        "NUM_WORKERS": str(req.num_workers),
        "NUM_ACTOR_VMS": str(req.num_actor_vms),
        "CONGESTION_SPEED_THRESHOLD": str(req.congestion_speed_threshold),
        "USE_GUI": "true" if req.use_gui else "false",
        **({"DISPLAY": ":99"} if req.use_gui else {}),
        "CAPTURE_TRAJECTORIES": "true" if req.capture_trajectories else "false",
        **({"SUMO_SEED": str(req.sumo_seed)} if req.sumo_seed is not None else {}),
        "TELEMETRY": "true" if req.telemetry else "false",
        "LOG_PROXIES": "true" if (req.telemetry if req.log_proxies is None else req.log_proxies) else "false",
        "JAM_TOP_EDGES": str(req.jam_top_edges),
        "JAM_FACTOR": str(req.jam_factor),
        "BROADCAST_RATE": str(req.broadcast_rate),
        "SPEED_EMA_ALPHA": str(req.speed_ema_alpha),
        "STATS_PRINT_INTERVAL": str(req.stats_print_interval),
        "KAFKA_BOOTSTRAP_SERVERS": req.kafka_bootstrap or KAFKA_BOOTSTRAP,
        "NETWORK_NAME": req.network_name,
        "GROUP_ID": req.group_id,
        **({"MAX_RUNTIME_S": str(req.max_runtime_s)} if req.max_runtime_s else {}),
        "QUEUE_ITEM_ID": req.queue_item_id or "",
        "REPEAT_INDEX": str(req.repeat_index),
        "REPEATS": str(req.repeats),
        "WARM_START": "true" if req.warm_start else "false",
        **({"CHECKPOINT_EXISTS_BEFORE": "true" if req.checkpoint_exists_before else "false"}
           if req.checkpoint_exists_before is not None else {}),
        **({"CHECKPOINT_TRAIN_STEPS_BEFORE": str(req.checkpoint_train_steps_before)}
           if req.checkpoint_train_steps_before is not None else {}),
        "PYTHONUNBUFFERED": "1",
    })

    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = PROJECT_ROOT + (os.pathsep + existing_pp if existing_pp else "")

    mgr.append_log("[agent] Starting simulation...")
    mgr.start_proc(
        [sys.executable, "layer1_simulation/run_simulation.py"],
        env=env, cwd=PROJECT_ROOT,
    )
    # Simulation is long-running — return immediately (fire-and-forget start)
    return LayerStatus(
        layer="layer1", status="running",
        pid=mgr.proc.pid if mgr.proc else None,
    )


@app.post("/stop")
def stop() -> StopResponse:
    mgr.stop()
    mgr.append_log("[agent] Simulation stopped.")
    return StopResponse(status="stopped")


@app.get("/status")
def status() -> LayerStatus:
    if mgr.is_running():
        return LayerStatus(layer="layer1", status="running", pid=mgr.proc.pid)
    return LayerStatus(layer="layer1", status="stopped")


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
    uvicorn.run(app, host="0.0.0.0", port=8001)
