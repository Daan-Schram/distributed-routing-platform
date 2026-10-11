"""
Samples CPU, RAM, and optionally GPU metrics for the current process and
publishes them to the resource_metrics Kafka topic every N seconds.
"""

import json
import os
import threading
import time

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

from common.config import KAFKA_BOOTSTRAP, TELEMETRY, TOPIC_RESOURCE_METRICS, VM_ID

MONITOR_INTERVAL = int(os.getenv("RESOURCE_MONITOR_INTERVAL", "5"))

try:
    import pynvml
    pynvml.nvmlInit()
    _NVML_AVAILABLE = True
except Exception:
    _NVML_AVAILABLE = False


class ResourceMonitor:
    def __init__(self, layer: str, process: str) -> None:
        self._layer = layer
        self._process = process
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._producer = None

    def start(self) -> None:
        if not _PSUTIL_AVAILABLE or not TELEMETRY:
            return
        from kafka import KafkaProducer
        try:
            self._producer = KafkaProducer(
                bootstrap_servers=[KAFKA_BOOTSTRAP],
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            )
        except Exception:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="resource-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._producer:
            try:
                self._producer.close(timeout=2)
            except Exception:
                pass

    def _gpu_metrics(self) -> tuple[float | None, float | None]:
        if not _NVML_AVAILABLE:
            return None, None
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            return float(util.gpu), mem.used / 1024 / 1024
        except Exception:
            return None, None

    def _run(self) -> None:
        proc = psutil.Process()
        proc.cpu_percent(interval=None)

        while not self._stop_event.wait(MONITOR_INTERVAL):
            try:
                cpu = proc.cpu_percent(interval=None)
                mem = proc.memory_info()
                gpu_pct, gpu_mem_mb = self._gpu_metrics()
                payload = {
                    "type": "resource",
                    "vm_id": VM_ID,
                    "layer": self._layer,
                    "process": self._process,
                    "cpu_pct": round(cpu, 1),
                    "ram_mb": round(mem.rss / 1024 / 1024, 1),
                    "ram_pct": round(proc.memory_percent(), 1),
                    "gpu_pct": round(gpu_pct, 1) if gpu_pct is not None else None,
                    "gpu_mem_mb": round(gpu_mem_mb, 1) if gpu_mem_mb is not None else None,
                    "net_in_mb": None,
                    "net_out_mb": None,
                    "timestamp": time.time(),
                }
                if self._producer:
                    self._producer.send(TOPIC_RESOURCE_METRICS, payload)
            except Exception:
                pass


def _parse_size_mb(s: str) -> float:
    s = s.strip()
    for suffix, factor in [
        ("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024),
        ("GB", 1000), ("MB", 1), ("kB", 1 / 1000), ("B", 1 / 1e6),
    ]:
        if s.endswith(suffix):
            return float(s[: -len(suffix)]) * factor
    return float(s)


class DockerResourceMonitor:
    """Polls `docker stats <container> --no-stream` and publishes to resource_metrics"""

    def __init__(self, container: str = "kafka") -> None:
        self._container = container
        self.paused = False
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._producer = None

    def start(self) -> None:
        if not TELEMETRY:
            return
        from kafka import KafkaProducer
        try:
            self._producer = KafkaProducer(
                bootstrap_servers=[KAFKA_BOOTSTRAP],
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            )
        except Exception:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="docker-resource-monitor"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._producer:
            try:
                self._producer.close(timeout=2)
            except Exception:
                pass

    def _run(self) -> None:
        import subprocess as _subprocess
        while not self._stop_event.wait(MONITOR_INTERVAL):
            if self.paused:
                continue
            try:
                result = _subprocess.run(
                    ["docker", "stats", self._container, "--no-stream", "--format", "{{json .}}"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0 or not result.stdout.strip():
                    continue
                d = json.loads(result.stdout.strip())
                cpu = float(d["CPUPerc"].rstrip("%"))
                ram_mb = _parse_size_mb(d["MemUsage"].split("/")[0])
                ram_pct = float(d["MemPerc"].rstrip("%"))
                net_parts = d.get("NetIO", "0B / 0B").split("/")
                net_in = _parse_size_mb(net_parts[0]) if len(net_parts) == 2 else None
                net_out = _parse_size_mb(net_parts[1]) if len(net_parts) == 2 else None
                payload = {
                    "type": "resource",
                    "vm_id": VM_ID,
                    "layer": "layer2",
                    "process": self._container,
                    "cpu_pct": round(cpu, 1),
                    "cpu_cores": os.cpu_count() or 1,
                    "ram_mb": round(ram_mb, 1),
                    "ram_pct": round(ram_pct, 1),
                    "gpu_pct": None,
                    "gpu_mem_mb": None,
                    "net_in_mb": round(net_in, 2) if net_in is not None else None,
                    "net_out_mb": round(net_out, 2) if net_out is not None else None,
                    "timestamp": time.time(),
                }
                if self._producer:
                    self._producer.send(TOPIC_RESOURCE_METRICS, payload)
            except Exception:
                pass
