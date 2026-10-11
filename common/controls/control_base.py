"""
Shared utilities for FastAPI control agents
"""
import collections
import json
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from typing import Optional

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, model_validator

import algorithms

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def make_api_key_dep(required_key: str):
    """
    Returns a FastAPI dependency that checks the X-API-Key header
    """
    def _check(key: str | None = Security(_api_key_header)):
        if required_key and key != required_key:
            raise HTTPException(status_code=403, detail="Forbidden")
    return Depends(_check)


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    algorithm: str = "dijkstra"
    hyperparams: dict[str, Any] = {}
    num_workers: int = 2
    num_actor_vms: int = 1
    congestion_speed_threshold: float = 1.0
    use_gui: bool = False
    broadcast_rate: int = 50
    speed_ema_alpha: float = 0.2
    stats_print_interval: int = 100
    restart_faust: bool = True
    reset_topics: bool = True
    kafka_bootstrap: str = ""
    network_name: str = "irregular_6x6"
    # If False the /start endpoint returns immediately without waiting for readiness
    wait: bool = True
    load_checkpoint: bool = False  # load saved model state from a previous run
    save_model: bool = True  # persist/overwrite the checkpoint during this run
    reset_checkpoint: bool = False
    capture_trajectories: bool = False  # debug: record per-vehicle edge paths for route/loop viz
    sumo_seed: Optional[int] = None  # None is SUMOss default seed
    telemetry: bool = True  # False: no resource monitors, no resource_metrics consumer
    log_proxies: Optional[bool] = None
    jam_top_edges: int = 0
    jam_factor: float = 12.0
    group_id: str = ""
    max_runtime_s: Optional[int] = None  # wall-clock timeout per experiment (0/None = no limit)
    queue_item_id: Optional[str] = None
    repeat_index: int = 1
    repeats: int = 1
    warm_start: bool = False
    checkpoint_exists_before: Optional[bool] = None
    checkpoint_train_steps_before: Optional[int] = None

    @model_validator(mode="after")
    def _resolve_hyperparams(self):
        self.hyperparams = algorithms.get(self.algorithm).resolve(self.hyperparams)
        return self


def algorithm_env(req: StartRequest) -> dict:
    return {"ALGORITHM": req.algorithm, "HYPERPARAMS": json.dumps(req.hyperparams)}


class LayerStatus(BaseModel):
    layer: str
    status: str # "running" | "stopped" | "starting" | "error"
    message: str = ""
    pid: int | None = None


class StopResponse(BaseModel):
    status: str
    message: str = ""


class ProcessManager:
    """Wraps a single subprocess with a log deque and thread-safe access"""

    def __init__(self, maxlen: int = 1000):
        self.proc: subprocess.Popen | None = None
        self.log_deque: collections.deque = collections.deque(maxlen=maxlen)
        self.lock = threading.Lock()

    def append_log(self, line: str) -> None:
        with self.lock:
            self.log_deque.append(line)

    def start_proc(self, cmd: list[str] | str, env=None, cwd=None) -> subprocess.Popen:
        """Start a subprocess and stream its stdout/stderr into log_deque"""
        with self.lock:
            self.log_deque.clear()
        self.proc = subprocess.Popen(
            cmd, env=env, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._read_logs, daemon=True).start()
        return self.proc

    def _read_logs(self) -> None:
        if self.proc is None or self.proc.stdout is None:
            return
        for line in self.proc.stdout:
            with self.lock:
                self.log_deque.append(line.rstrip())

    def stop(self) -> None:
        """Terminate the managed process"""
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        self.proc = None

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def has_exited(self) -> bool:
        return self.proc is not None and self.proc.poll() is not None

    def wait_for_sentinel(self, sentinel: str, timeout: int) -> bool:
        """Block until sentinel appears in the log deque or until timeout or process exit."""
        deadline = time.time() + timeout
        seen = 0
        while time.time() < deadline:
            with self.lock:
                lines = list(self.log_deque)
            for line in lines[seen:]:
                if sentinel in line:
                    return True
            seen = len(lines)
            if not self.is_running():
                return False
            time.sleep(0.2)
        return False


def make_sse_generator(mgr: ProcessManager) -> Callable:
    """
    Returns a generator function with SSE-formatted lines from mgr's log deque.

    A heartbeat comment (': hb') is sent every ~5 s so that broken TCP connections are detected
    """
    def generate():
        sent = 0
        idle_ticks = 0
        hb = 0
        has_sent_exit_sentinel = False

        while True:
            with mgr.lock:
                lines = list(mgr.log_deque)
            
            if len(lines) < sent:
                sent = 0 # deque was cleared (new experiment started)
                has_sent_exit_sentinel = False

            new = lines[sent:]
            for line in new:
                yield f"data: {line}\n\n"
            sent = len(lines)

            if mgr.has_exited() and not new:
                if not has_sent_exit_sentinel:
                    idle_ticks += 1
                    if idle_ticks >= 10: # 1s drain window
                        yield "data: [process exited]\n\n"
                        has_sent_exit_sentinel = True
            else:
                idle_ticks = 0
            
            hb += 1
            if hb >= 50: # 5 s
                hb = 0
                yield ": hb\n\n"
            time.sleep(0.1)

    return generate
