"""
Docker resource monitor
Run this on the machine hosting Docker/Kafka so it publishes
container stats to the resource_metrics Kafka topic
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.monitoring.resource_monitor import DockerResourceMonitor

monitor = DockerResourceMonitor(container="kafka")
monitor.start()
print(f"[DockerMonitor] Publishing kafka container stats to resource_metrics "
      f"(VM_ID={os.getenv('VM_ID', 'local')})")
print("[DockerMonitor] Press Ctrl+C to stop.")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    monitor.stop()
    print("[DockerMonitor] Stopped.")
