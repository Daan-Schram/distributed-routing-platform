import importlib
import sys
import time

import algorithms
from common.config import KAFKA_BOOTSTRAP


def wait_for_kafka():
    MAX_KAFKA_ATTEMPTS = 20
    for attempt in range(1, MAX_KAFKA_ATTEMPTS + 1):
        try:
            from kafka.admin import KafkaAdminClient
            admin = KafkaAdminClient(bootstrap_servers=[KAFKA_BOOTSTRAP], request_timeout_ms=5000)
            admin.list_topics()
            admin.close()
            return
        except Exception:
            print(f"  Kafka not ready yet (attempt {attempt}), retrying in 3s...")
            time.sleep(3)
    print("ERROR: Kafka did not become ready after max attempts.")
    sys.exit(1)


algo, hp = algorithms.resolve_env()

print("Waiting for Kafka...")
wait_for_kafka()
print("Connected to Kafka.\n")

print(f"Starting Layer 4 learner for algorithm: {algo.name}")
importlib.import_module(algo.learner or "layer4_learner.null_learner").run(hp)
