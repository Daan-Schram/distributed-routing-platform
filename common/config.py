import json
import os
from pathlib import Path

# Load .env from project root (for VMs, in dev ignored)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

_DATA_DIR = Path(__file__).parent/"data"

NETWORK_NAME = os.getenv("NETWORK_NAME", "irregular_6x6")
NETWORK_FILE = str(_DATA_DIR / "networks" / f"{NETWORK_NAME}.net.xml")
ROUTE_FILE   = str(_DATA_DIR / "routes"   / f"{NETWORK_NAME}.rou.xml")


def list_available_networks() -> list:
    """Return list of networks"""
    return sorted(p.name.removesuffix(".net.xml") for p in (_DATA_DIR / "networks").glob("*.net.xml"))


def network_file_path(name: str) -> str:
    return str(_DATA_DIR / "networks" / f"{name}.net.xml")


def route_file_path(name: str) -> str:
    return str(_DATA_DIR / "routes" / f"{name}.rou.xml")


def list_available_networks_with_meta() -> list[dict]:
    """Return networks with metadata from .meta.json"""
    result = []
    for p in sorted((_DATA_DIR / "networks").glob("*.net.xml")):
        name = p.name.removesuffix(".net.xml")
        meta_path = p.parent / f"{name}.meta.json"
        duration = None
        period = None
        phases = None
        if meta_path.is_file():
            try:
                with open(meta_path) as f:
                    data = json.load(f)
                duration = data.get("duration")
                period = data.get("period")
                phases = data.get("phases")
            except Exception:
                pass
        result.append({"name": name, "duration": duration, "period": period, "phases": phases})
    return result


KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
VM_ID = os.getenv("VM_ID", "local")

TOPIC_TRAFFIC_STATE = "traffic_state"
TOPIC_ROUTE_REQUESTS = "route_requests"
TOPIC_ROUTE_RESPONSES = "route_responses"
TOPIC_EDGE_TRANSITIONS = "edge_transitions"
TOPIC_EXPERIENCE_REPLAYS = "experience_replays"
TOPIC_MODEL_WEIGHTS = "model_weights"
TOPIC_METRICS = "metrics"
TOPIC_RESOURCE_METRICS = "resource_metrics"

# All application topics, used by start_faust.py to reset state between experiments
ALL_TOPICS = [
    TOPIC_TRAFFIC_STATE, TOPIC_ROUTE_REQUESTS, TOPIC_ROUTE_RESPONSES,
    TOPIC_EDGE_TRANSITIONS, TOPIC_EXPERIENCE_REPLAYS, TOPIC_MODEL_WEIGHTS,
    TOPIC_METRICS, TOPIC_RESOURCE_METRICS,
]

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
_OUTPUT_ROOT = Path(__file__).resolve().parent.parent / OUTPUT_DIR
EXPERIMENTS_DIR = str(_OUTPUT_ROOT / "experiments")
CHECKPOINTS_DIR = str(_OUTPUT_ROOT / "checkpoints")
STATE_DIR = str(_OUTPUT_ROOT / "state")
TRAJECTORIES_DIR = str(_OUTPUT_ROOT / "trajectories")
INSTRUMENTATION_DIR = str(_OUTPUT_ROOT / "instrumentation")
RUN_ID_FILE = str(_OUTPUT_ROOT / ".current_run_id")

CONGESTION_SPEED_THRESHOLD = float(os.getenv("CONGESTION_SPEED_THRESHOLD", "1.0"))  # m/s

# Layer 1 config
USE_GUI = os.getenv("USE_GUI", "false").lower() == "true"
CAPTURE_TRAJECTORIES = os.getenv("CAPTURE_TRAJECTORIES", "false").lower() == "true"
SUMO_SEED = int(os.environ["SUMO_SEED"]) if os.getenv("SUMO_SEED") else None  # None = SUMO's built-in default seed
BROADCAST_RATE = int(os.getenv("BROADCAST_RATE", "50"))  # Broadcast traffic_state every N steps
SPEED_EMA_ALPHA = float(os.getenv("SPEED_EMA_ALPHA", "0.2"))  # EMA on broadcast edge speed. 1.0 = immediately
STATS_PRINT_INTERVAL = int(os.getenv("STATS_PRINT_INTERVAL", "100"))  # Print latency summary every N steps
JAM_TOP_EDGES = int(os.getenv("JAM_TOP_EDGES", "0"))
JAM_FACTOR = float(os.getenv("JAM_FACTOR", "12.0"))

# Layer 3 config
NUM_WORKERS = int(os.getenv("NUM_WORKERS", "2"))
FAUST_APP_ID = "traffic_optimizer_agents"
LEARNER_GROUP_ID = "layer4_learner"

# Metrics config
TELEMETRY = os.getenv("TELEMETRY", "true").lower() != "false"  # false: no resource monitors / resource_metrics consumer
SNAPSHOT_INTERVAL = int(os.getenv("SNAPSHOT_INTERVAL", "60"))

# Control agent URLs, used by dashboard to communicate with per VM agents
AGENT_LAYER1_URL = os.getenv("AGENT_LAYER1_URL", "http://localhost:8001")
AGENT_LAYER2_URL = os.getenv("AGENT_LAYER2_URL", "http://localhost:8002")
AGENT_LAYER3_URL = os.getenv("AGENT_LAYER3_URL", "http://localhost:8003")
AGENT_LAYER4_URL = os.getenv("AGENT_LAYER4_URL", "http://localhost:8004")
AGENT_LAYER1B_URL = os.getenv("AGENT_LAYER1B_URL", "")
AGENT_LAYER3_EXTRA_URLS = os.getenv("AGENT_LAYER3_EXTRA_URLS", "")

# Shared secret for communication between agents, exists in every VMs .env
# Empty string disables auth (local dev)
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
