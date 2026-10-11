from common.config import TOPIC_METRICS


def publish_metrics(producer, run_id: str, algorithm: str, step: int, values: dict, state: dict) -> None:
    producer.send(TOPIC_METRICS, {"type": "learner_metrics", "run_id": run_id, "algorithm": algorithm,
                                  "step": step, "values": values, "learner_state": state})


def publish_pressure(producer, run_id: str, poll_count: int, cap: int, state: dict) -> None:
    producer.send(TOPIC_METRICS, {"type": "training_pressure", "run_id": run_id, "poll_count": poll_count,
                                  "cap": cap, "learner_state": state})
