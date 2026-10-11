"""
Null learner for the Dijkstra and dynamic Dijkstra benchmarks.
Consumes experience_replays without learning. This is used to drain the topic
and collect tuple counts for benchmark validation.
"""

import json
import os
from kafka import KafkaConsumer

from common.config import KAFKA_BOOTSTRAP, TOPIC_EXPERIENCE_REPLAYS, LEARNER_GROUP_ID
from common.monitoring.resource_monitor import ResourceMonitor


def run(hp: dict) -> None:
    """Consume experience tuples and print periodic throughput stats"""
    monitor = ResourceMonitor(layer="layer4", process="null_learner")
    monitor.start()
    consumer = KafkaConsumer(
        TOPIC_EXPERIENCE_REPLAYS,
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        auto_offset_reset="latest",
        group_id=LEARNER_GROUP_ID,
    )
    print("[Learner] Ready")

    count = 0
    for _ in consumer:
        count += 1
        if count % 500 == 0:
            print(f"[NullLearner] {count} experience tuples received ({os.getenv('ALGORITHM')}, no learning)")
