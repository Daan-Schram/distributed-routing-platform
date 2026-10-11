import os
import subprocess
import sys
import time

LAYER2_DIR = os.path.join(os.path.dirname(__file__), "..", "layer2_message_bus")


def run(cmd, cwd=None, capture=False):
    return subprocess.run(
        cmd, cwd=cwd, shell=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


print("Starting Kafka message bus (Docker)...")
result = run("docker compose up -d", cwd=LAYER2_DIR)
if result.returncode != 0:
    print("ERROR: 'docker compose up -d' failed. Is Docker Desktop running?")
    sys.exit(1)
print("Containers started. Waiting for Kafka broker...")

attempts = 0
while True:
    result = run(
        "docker exec kafka kafka-topics --bootstrap-server localhost:9092 --list",
        capture=True,
    )
    if result.returncode == 0:
        break
    attempts += 1
    print(f"  Not ready yet (attempt {attempts}), retrying in 3s...")
    time.sleep(3)

print("Kafka is ready!")
