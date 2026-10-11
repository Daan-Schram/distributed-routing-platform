import collections
import json
import logging
import os
import time
from pathlib import Path
import asyncio

import faust
import networkx as nx
from aiokafka import AIOKafkaConsumer

import algorithms
from common.config import (
    NETWORK_FILE, KAFKA_BOOTSTRAP,
    TOPIC_TRAFFIC_STATE, TOPIC_ROUTE_REQUESTS, TOPIC_ROUTE_RESPONSES,
    TOPIC_EDGE_TRANSITIONS, TOPIC_EXPERIENCE_REPLAYS, TOPIC_MODEL_WEIGHTS,
    TOPIC_METRICS, RUN_ID_FILE, NUM_WORKERS, FAUST_APP_ID
)
from common.networking.network import load_network
from common.monitoring.resource_monitor import ResourceMonitor

# Edges used in NetworkState
class EdgeData(faust.Record):
    edge_id: str
    vehicle_count: int
    mean_speed: float

# Network State for state_topic
class NetworkState(faust.Record):
    step: int
    timestamp: float
    network_state: list[EdgeData]

# Route Request for request_topic
class RouteRequest(faust.Record):
    vehicle_id: str
    current_edge: str
    destination: str
    request_time: float

# Route Response for response_topic
class RouteResponse(faust.Record):
    vehicle_id: str
    current_edge: str  # Edge the decision was made for — used by Layer 1 to discard stale responses
    next_edge: str
    decision_reason: str
    request_time: float

# Edge Transition for edge_transitions_topic
class EdgeTransition(faust.Record):
    vehicle_id: str
    prev_edge: str
    new_edge: str
    travel_time: float
    dest_edge: str
    sim_time: float

# Experience Replay for experience_replays_topic
class ExperienceReplay(faust.Record):
    vehicle_id: str
    prev_edge: str
    new_edge: str
    state_before: dict
    action: str
    travel_time: float
    state_after: dict
    dest_edge: str
    sim_time: float
    is_revisit: bool = False
    revisit_count: int = 0

logger = logging.getLogger(__name__)

class _SuppressProducerBufferFull(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "producer buffer full" not in record.getMessage()

_stale_decision_count = 0
_unknown_vehicle_count = 0
_WARN_BATCH = 50  # log first occurrence, then every Nth

# Per-trip path tracking: revisit flags for the experience replays and a periodic path log
_path_table: dict[str, dict] = {}  # vid → {start_edge, dest_edge, visits: Counter, hops: int}
_arrivals_logged = 0
_LOG_EVERY_N_ARRIVALS = 5  # print every N vehicle arrivals


def _dijkstra_path(start_edge: str, dest_edge: str) -> list[str]:
    try:
        return nx.shortest_path(_line_graph, start_edge, dest_edge, weight="time")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []

app = faust.App(
    FAUST_APP_ID,
    broker=f"kafka://{KAFKA_BOOTSTRAP}",
    value_serializer="json",
    topic_partitions=NUM_WORKERS,
    broker_session_timeout=10,
    broker_rebalance_timeout=60.0,
    broker_heartbeat_interval=1.0,
    producer_linger_ms=5,
    producer_compression_type="gzip",
)

# traffic_state: single partition, GlobalTable replicates all partitions to every worker
state_topic = app.topic(TOPIC_TRAFFIC_STATE, value_type=NetworkState, partitions=1)
# route_requests + edge_transitions: keyed by vehicle_id for per-worker locality
request_topic = app.topic(TOPIC_ROUTE_REQUESTS, value_type=RouteRequest, key_type=str)
response_topic = app.topic(TOPIC_ROUTE_RESPONSES, value_type=RouteResponse)
edge_transitions_topic = app.topic(TOPIC_EDGE_TRANSITIONS, value_type=EdgeTransition, key_type=str)
experience_replays_topic = app.topic(TOPIC_EXPERIENCE_REPLAYS, value_type=ExperienceReplay)
# model_weights: 1 partition
model_weights_topic = app.topic(TOPIC_MODEL_WEIGHTS, partitions=1)
metrics_topic = app.topic(TOPIC_METRICS) # plain JSON

# GlobalTable, faust replicates all partitions to every worker
traffic_map = app.GlobalTable("traffic_map", default=dict)
# same worker handles process_route_requests and process_edge_transitions for a vehicle
decision_table: dict[str, dict] = {}


_graph, _line_graph, _edge_map, _connection_map = load_network(NETWORK_FILE)

_ordered_edges: list[str] = list(_edge_map)
_FALLBACK_SPEED = 14.0  # only for zero-length/zero-time edges
# Per-edge free-flow speed (length/time = lane speed limit); missing-edge default.
_freeflow_speeds: list[float] = [
    (_edge_map[e][2] / _edge_map[e][3]) if _edge_map[e][3] > 0 else _FALLBACK_SPEED
    for e in _ordered_edges
]

_resource_monitor = ResourceMonitor(layer="layer3", process="agent_worker")
_resource_monitor.start()

_algo, _hp = algorithms.resolve_env()
_router = _algo.make_router(_line_graph, _edge_map, _connection_map, _hp)

# run_id reader, gets filled once Layer 1 writes the .current_run_id file
_run_id: str = "unknown"

def _get_run_id() -> str:
    global _run_id
    if _run_id == "unknown":
        p = Path(RUN_ID_FILE)
        if p.exists():
            _run_id = p.read_text().strip()
    return _run_id


def _full_state() -> dict:
    """Full-network speed/count as parallel arrays in edge order.

    Matches the inference snapshot so train/inference feature distributions are the same.
    Unseen edges default to per-edge free-flow speed (not 0, which reads as fully jammed).
    """
    speeds = list(_freeflow_speeds)
    counts = [0.0] * len(_ordered_edges)
    for i, eid in enumerate(_ordered_edges):
        entry = traffic_map.get(eid)
        if entry:
            speeds[i] = entry.get("speed", _freeflow_speeds[i])
            counts[i] = entry.get("count", 0.0)
    return {"speeds": speeds, "counts": counts}


_router_snapshot_key = None


def _sync_router_snapshot(state: dict) -> None:
    """Feed the router from the replicated traffic_map, only when it changed"""
    global _router_snapshot_key
    key = (tuple(state["speeds"]), tuple(state["counts"]))
    if key == _router_snapshot_key:
        return
    _router_snapshot_key = key
    _router.update_traffic_state({
        e: {"speed": s, "count": c}
        for e, s, c in zip(_ordered_edges, state["speeds"], state["counts"])
    })


@app.agent(state_topic)
async def process_traffic_state(stream):
    """Maintain traffic_map with the latest per-edge speed and vehicle count, coming from layer 1"""
    async for update in stream:
        for edge in update.network_state:
            traffic_map[edge.edge_id] = {
                "speed": edge.mean_speed,
                "count": edge.vehicle_count,
                "last_update": update.step,
            }


@app.agent(request_topic)
async def process_route_requests(stream):
    """Make a routing decision for each incoming request, store state_before for experience replay"""
    async for req in stream:
        t_recv = time.time()
        if not req.destination:
            continue

        if req.current_edge.startswith(":"):
            continue

        # Vehicle is already on its destination edge
        if req.current_edge == req.destination:
            await response_topic.send(value=RouteResponse(
                vehicle_id=req.vehicle_id,
                current_edge=req.current_edge,
                next_edge=req.current_edge,
                decision_reason="at_destination",
                request_time=req.request_time,
            ))
            continue

        t_prep = time.perf_counter()
        state_before = _full_state()

        if req.vehicle_id not in _path_table:
            _path_table[req.vehicle_id] = {
                "start_edge": req.current_edge,
                "dest_edge": req.destination,
                "visits": collections.Counter((req.current_edge,)),
                "hops": 0,
            }

        if _algo.needs_snapshot:
            _sync_router_snapshot(state_before)

        t_compute = time.perf_counter()
        decision = await asyncio.to_thread(_router.find_next_edge, req.current_edge, req.destination, req.vehicle_id)
        t_done = time.perf_counter()

        if not decision:
            decision = req.current_edge

        decision_table[req.vehicle_id] = {
            "state_before": state_before,
            "action": decision,
            "dest_edge": req.destination,
            "decision_edge": req.current_edge,
        }

        # print(f"Current edge: {req.current_edge}, Decision: {decision}, Destination: {req.destination}")

        await metrics_topic.send(value={
            "type": "decision_latency",
            "latency_s": time.time() - req.request_time,
            "transport_s": t_recv - req.request_time,
            "prep_s": t_compute - t_prep,
            "compute_s": t_done - t_compute,
            "run_id": _get_run_id(),
        })

        await response_topic.send(value=RouteResponse(
            vehicle_id=req.vehicle_id,
            current_edge=req.current_edge,
            next_edge=decision,
            decision_reason=_algo.name,
            request_time=req.request_time,
        ))


@app.agent(edge_transitions_topic)
async def process_edge_transitions(stream):
    """Combine an edge transition with the stored routing decision to push an experience tuple"""
    global _stale_decision_count, _unknown_vehicle_count
    async for trans in stream:
        entry = decision_table.get(trans.vehicle_id)
        if not entry:
            _unknown_vehicle_count += 1
            if _unknown_vehicle_count % _WARN_BATCH == 1:
                logger.warning(
                    "drop:unknown_vehicle x%d (latest: vid=%s prev=%s)",
                    _unknown_vehicle_count, trans.vehicle_id, trans.prev_edge,
                )
            continue

        # Prevents use of outdated decision
        if entry.get("decision_edge") != trans.prev_edge:
            _stale_decision_count += 1
            if _stale_decision_count % _WARN_BATCH == 1:
                logger.warning(
                    "drop:stale_decision x%d (latest: vid=%s decision_edge=%s prev=%s)",
                    _stale_decision_count, trans.vehicle_id, entry.get("decision_edge"), trans.prev_edge,
                )
            continue

        state_after = _full_state()

        await metrics_topic.send(value={
            "type": "edge_travel",
            "travel_time_s": trans.travel_time,
            "run_id": _get_run_id(),
        })

        # Loop signal, check if vehicle already driven new_edge for this trip
        is_revisit = False
        revisit_count = 0
        _pi = _path_table.get(trans.vehicle_id)
        if _pi is not None:
            revisit_count = _pi["visits"][trans.new_edge]
            is_revisit = revisit_count > 0

        await experience_replays_topic.send(value=ExperienceReplay(
            vehicle_id=trans.vehicle_id,
            prev_edge=trans.prev_edge,
            new_edge=trans.new_edge,
            state_before=entry["state_before"],
            action=entry["action"],
            travel_time=trans.travel_time,
            state_after=state_after,
            dest_edge=trans.dest_edge,
            sim_time=trans.sim_time,
            is_revisit=is_revisit,
            revisit_count=revisit_count,
        ))

        # Vehicle reached its destination
        if trans.new_edge == entry.get("dest_edge"):
            _router.clear_vehicle(trans.vehicle_id)

            global _arrivals_logged
            path_info = _path_table.pop(trans.vehicle_id, None)
            if path_info:
                path_info["hops"] += 1  # include the hop onto the destination
                _arrivals_logged += 1
                if _arrivals_logged % _LOG_EVERY_N_ARRIVALS == 0:
                    dijkstra = _dijkstra_path(path_info["start_edge"], path_info["dest_edge"])
                    print(
                        f"[path #{_arrivals_logged}] vid={trans.vehicle_id} "
                        f"{path_info['start_edge']} -> {path_info['dest_edge']} | "
                        f"driven {path_info['hops']} hops | "
                        f"dijkstra {len(dijkstra) - 1} hops"
                    )

        elif trans.vehicle_id in _path_table:
            path_info = _path_table[trans.vehicle_id]
            path_info["visits"][trans.new_edge] += 1
            path_info["hops"] += 1


@app.task
async def _suppress_noisy_logs():
    _f = _SuppressProducerBufferFull()
    for _h in logging.root.handlers:
        _h.addFilter(_f)
    logging.root.addFilter(_f)


@app.task
async def consume_model_weights():
    """Broadcast consumer: each worker gets its own consumer group so all receive every delta."""
    unique_group = f"model_weights_worker_{os.getpid()}"
    consumer = AIOKafkaConsumer(
        TOPIC_MODEL_WEIGHTS,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id=unique_group,
        auto_offset_reset="latest",
        value_deserializer=lambda m: json.loads(m.decode()),
    )
    await consumer.start()
    try:
        async for msg in consumer:
            try:
                if msg.value.get("algorithm") == _algo.name:
                    _router.apply_update(msg.value)
            except Exception as e:
                print(f"[agent_worker] model_weights message skipped: {type(e).__name__}: {e}")
    finally:
        await consumer.stop()
