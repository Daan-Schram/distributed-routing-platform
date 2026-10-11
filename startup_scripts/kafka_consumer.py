import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.config import KAFKA_BOOTSTRAP


def wait_for_kafka():
    attempts = 0
    while True:
        result = subprocess.run(
            f"docker exec kafka kafka-topics --bootstrap-server {KAFKA_BOOTSTRAP} --list",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode == 0:
            return
        attempts += 1
        print(f"  Kafka not ready yet (attempt {attempts}), retrying in 3s...")
        time.sleep(3)


print("Waiting for Kafka...")
wait_for_kafka()
# print("Connected. Logging turned off. Uncomment in kafka_consumer.py to see logs.\n")
print("Connected. Consuming all...\n")

result = subprocess.run(
    f"docker exec kafka kafka-console-consumer "
    f"--bootstrap-server {KAFKA_BOOTSTRAP} "
    f"--include .*",
    shell=True,
)
sys.exit(result.returncode)
