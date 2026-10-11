import os
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.config import (
    NUM_WORKERS, ALL_TOPICS, FAUST_APP_ID, KAFKA_BOOTSTRAP,
    TOPIC_ROUTE_REQUESTS, TOPIC_EDGE_TRANSITIONS, TOPIC_RESOURCE_METRICS,
)

NUM_ACTOR_VMS = int(os.getenv("NUM_ACTOR_VMS", "1"))
TOTAL_WORKERS = NUM_WORKERS * NUM_ACTOR_VMS

SKIP_RESET = os.getenv("FAUST_SKIP_RESET", "0") == "1"

_procs: list = []


def _shutdown(signum, frame):
    """Forward termination to Faust worker children when this process is killed"""
    for p in _procs:
        if p.poll() is None:
            p.terminate()
    sys.exit(0)


signal.signal(signal.SIGTERM, _shutdown)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_admin():
    from kafka.admin import KafkaAdminClient
    from kafka.errors import NodeNotReadyError

    class _RobustAdminClient(KafkaAdminClient):
        def _refresh_controller_id(self):
            original = self._client.check_version

            def _check_with_retry(node_id):
                self._client.maybe_connect(node_id)
                for _ in range(10):
                    try:
                        return original(node_id)
                    except NodeNotReadyError:
                        self._client.poll(timeout_ms=300)
                return original(node_id)

            self._client.check_version = _check_with_retry
            try:
                return super()._refresh_controller_id()
            finally:
                self._client.check_version = original

    return _RobustAdminClient(
        bootstrap_servers=[KAFKA_BOOTSTRAP],
        request_timeout_ms=10000,
    )


def _list_kafka_topics() -> list[str]:
    admin = _get_admin()
    try:
        return list(admin.list_topics())
    finally:
        admin.close()


def _delete_topics(topics: list[str]) -> None:
    if not topics:
        return
    admin = _get_admin()
    try:
        admin.delete_topics(topics)
    finally:
        admin.close()


def _create_topics(topic_map: dict[str, int], retries: int = 10) -> None:
    from kafka.admin import NewTopic
    from kafka.errors import TopicAlreadyExistsError
    remaining = dict(topic_map)
    for attempt in range(1, retries + 1):
        admin = _get_admin()
        try:
            new_topics = [
                NewTopic(name=name, num_partitions=parts, replication_factor=1)
                for name, parts in remaining.items()
            ]
            admin.create_topics(new_topics)
            return  # All created successfully
        except TopicAlreadyExistsError:
            existing = set(_list_kafka_topics())
            remaining = {t: p for t, p in remaining.items() if t not in existing}
            if not remaining:
                return  # All topics now confirmed present
            if attempt < retries:
                print(f"  {len(remaining)} topic(s) still pending, retrying in 2s (attempt {attempt}/{retries})...")
                time.sleep(2)
            else:
                print("  Warning: could not create all topics after retries.")
        finally:
            admin.close()


def _reset_state() -> None:
    """
    Delete all application Kafka topics and Faust's local DB state so that
    each experiment starts from a completely clean slate
    """
    existing = set(_list_kafka_topics())

    to_delete = [t for t in ALL_TOPICS if t in existing and t != TOPIC_RESOURCE_METRICS]
    to_delete += [t for t in existing if t.startswith(FAUST_APP_ID)]

    if to_delete:
        print(f"  Deleting {len(to_delete)} topic(s): {', '.join(to_delete)}")
        _delete_topics(to_delete)
    else:
        print("  No existing topics to delete.")

    state_dir = PROJECT_ROOT + f"/{FAUST_APP_ID}-data"
    if os.path.exists(state_dir):
        shutil.rmtree(state_dir)
        print(f"  Removed local state: {state_dir}")


print(f"Waiting for Kafka to be ready... (bootstrap: {KAFKA_BOOTSTRAP})")
MAX_KAFKA_ATTEMPTS = 10
for attempt in range(1, MAX_KAFKA_ATTEMPTS + 1):
    try:
        admin = _get_admin()
        topics = admin.list_topics()
        admin.close()
        print(f"Kafka ready. Existing topics: {sorted(topics) or '(none)'}")
        break
    except Exception as e:
        print(f"  Kafka not ready yet (attempt {attempt}/{MAX_KAFKA_ATTEMPTS}): {type(e).__name__}: {e}")
        if attempt < MAX_KAFKA_ATTEMPTS:
            time.sleep(3)
else:
    print("ERROR: Kafka did not become ready after max attempts.")
    sys.exit(1)

if SKIP_RESET:
    print("Skipping state reset (FAUST_SKIP_RESET=1).")
else:
    print("Resetting state for clean experiment run...")
    _reset_state()

WORKER_PARTITIONED = {TOPIC_ROUTE_REQUESTS, TOPIC_EDGE_TRANSITIONS}

# Changelog topics Faust would otherwise auto-create with wrong partition counts
# GlobalTable traffic_map sources from traffic_state (1 partition), so must be 1
CHANGELOG_TOPICS = {
    f"{FAUST_APP_ID}-traffic_map-changelog": 1,
}


def _wait_for_deletion(topics: list[str], timeout: int = 15) -> None:
    print("  Waiting for topic deletions to propagate...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        existing = set(_list_kafka_topics())
        if not any(t in existing for t in topics):
            # Extra pause — Kafka may still be finalizing deletion internally
            time.sleep(2)
            return
        time.sleep(1)
    print("  Warning: some topics may not have been fully deleted (continuing).")


print("Pre-creating Kafka topics...")
_wait_for_deletion(list(ALL_TOPICS) + list(CHANGELOG_TOPICS))

all_to_create = {
    **{t: (TOTAL_WORKERS if t in WORKER_PARTITIONED else 1) for t in ALL_TOPICS},
    **CHANGELOG_TOPICS,
}
print(f"  Creating {len(all_to_create)} topic(s) ({NUM_WORKERS} workers * {NUM_ACTOR_VMS} VM(s) = {TOTAL_WORKERS} partitions for routed topics)...")
_create_topics(all_to_create)
print("[Faust] Topics ready")

print(f"Starting {NUM_WORKERS} Faust actor worker(s)...")
procs = [
    subprocess.Popen(
        [sys.executable, "-m", "faust", "-A", "layer3_actors.agent_worker", "worker", "-l", "warn", "--without-web"],
        cwd=PROJECT_ROOT,
    )
    for _ in range(NUM_WORKERS)
]
_procs.extend(procs)

try:
    for p in procs:
        p.wait()
except KeyboardInterrupt:
    for p in procs:
        p.terminate()

sys.exit(max(p.returncode or 0 for p in procs))
