import logging
import time

import traci
import traci.constants as tc

from common.config import (
    TOPIC_TRAFFIC_STATE,
    TOPIC_ROUTE_REQUESTS,
    TOPIC_ROUTE_RESPONSES,
    TOPIC_EDGE_TRANSITIONS,
    SPEED_EMA_ALPHA,
)
from layer1_simulation.latency_tracker import LatencyTracker

MAX_LATENCY_THRESHOLD = 2.0

KAFKA_TOPIC_RESPONSES = TOPIC_ROUTE_RESPONSES

logger = logging.getLogger(__name__)
_ROUTE_WARN_BATCH = 10  # log first occurrence, then every Nth


class SimulationBridge:
    """
    All SUMO to Kafka communication is done in here.
    There are three outbound topics (traffic_state, route_requests, edge_transitions)
    and one inbound (route_responses).

    State held per vehicle:
      _pending_requests       — vehicles with an outstanding route request
      _vehicle_request_status — deduplication: last edge a request was sent for
      _vehicle_destination    — cached final destination edge per vehicle
      _vehicle_last_edge      — (edge_id, sumo_time) when the vehicle entered its current edge
      _lane_length_cache      — cached lane lengths to avoid redundant TraCI calls
    """

    def __init__(self, producer, consumer, metrics=None):
        self._producer = producer
        self._consumer = consumer
        self._tracker  = LatencyTracker()
        self._metrics  = metrics
        self._pending_requests: dict[str, float] = {} # Vehicle ID to request time
        self._vehicle_request_status: dict[str, str] = {} # Vehicle ID to last current edge from which a request was made
        self._vehicle_destination: dict[str, str] = {} # Vehicle ID to final destination edge
        self._vehicle_last_edge: dict[str, tuple[str, float]] = {} # Vehicle ID to (edge_id, sumo_time)
        self._lane_length_cache: dict[str, float] = {} # Lane ID to lane length
        self._edge_freeflow_cache: dict[str, float] = {} # Edge ID to lane speed limit
        self._ema_speed: dict[str, float] = {} # Edge ID to smoothed mean speed
        self._total_requests_sent: int = 0
        self._responses_dropped_vehicle_gone: int = 0
        self._responses_already_at_edge: int = 0  # Response arrived after vehicle was already on next_edge
        self._duplicate_requests: int = 0  # Skipped sends because a previous request for this vehicle was still pending
        self._premature_arrivals: int = 0  # Vehicles whose last known edge != cached destination at arrival time
        self._route_tail_cache: dict[tuple[str, str], tuple[str, ...]] = {}  # (next_edge, dest_edge) -> edges from next_edge (exclusive) to dest_edge
        self._route_no_connection_count: int = 0
        self._route_junction_internal_count: int = 0
        self._route_other_error_count: int = 0
        self._teleports_started: int = 0
        self._teleports_ended: int = 0

    def register_vehicle(self, veh_id: str) -> None:
        """Cache the destination edge for a newly departed vehicle."""
        route = traci.vehicle.getRoute(veh_id)
        if route:
            self._vehicle_destination[veh_id] = route[-1]

    def _edge_freeflow(self, edge_id: str) -> float:
        if edge_id not in self._edge_freeflow_cache:
            try:
                self._edge_freeflow_cache[edge_id] = traci.lane.getMaxSpeed(f"{edge_id}_0")
            except traci.TraCIException:
                self._edge_freeflow_cache[edge_id] = 13.89
        return self._edge_freeflow_cache[edge_id]

    def _observed_speed(self, edge_id: str, count: int) -> float:
        """Smoothed mean speed. SUMO reports 0 m/s for an empty edge, which downstream
        reads as fully jammed, so empty edges are reported at free flow"""
        raw = traci.edge.getLastStepMeanSpeed(edge_id) if count > 0 else self._edge_freeflow(edge_id)
        if SPEED_EMA_ALPHA >= 1.0:
            return raw
        prev = self._ema_speed.get(edge_id)
        smoothed = raw if prev is None else SPEED_EMA_ALPHA * raw + (1.0 - SPEED_EMA_ALPHA) * prev
        self._ema_speed[edge_id] = smoothed
        return smoothed

    def broadcast_state(self, all_edges, step: int) -> None:
        """Publish per edge traffic state (vehicle count, mean speed) to traffic_state"""
        network_state = []
        for edge_id in all_edges:
            if edge_id.startswith(":"):
                continue
            count = traci.edge.getLastStepVehicleNumber(edge_id)
            network_state.append({
                "edge_id": edge_id,
                "vehicle_count": count,
                "mean_speed": self._observed_speed(edge_id, count),
            })
        self._producer.send(TOPIC_TRAFFIC_STATE, {
            "step": step,
            "timestamp": time.time(),
            "network_state": network_state,
        })

    def check_and_send_requests(self, step: int, vehicle_data: dict) -> None:
        """Send a route_request for each vehicle within 50 m of its next junction"""
        messages_sent = False

        for veh_id, data in vehicle_data.items():
            try:
                current_edge = data[tc.VAR_ROAD_ID]
                current_lane = data[tc.VAR_LANE_ID]

                if not current_lane or not current_edge or current_edge.startswith(":"):
                    continue

                if current_lane not in self._lane_length_cache:
                    self._lane_length_cache[current_lane] = traci.lane.getLength(current_lane)

                lane_len = self._lane_length_cache[current_lane]
                lane_pos = max(0, data[tc.VAR_LANEPOSITION])

                # If request (Vehicle ID, edge) already exists, continue
                if veh_id in self._vehicle_request_status and self._vehicle_request_status[veh_id] == current_edge:
                    continue

                # If vehicle is within 50m of next junction, send a new route request
                if lane_len - lane_pos < 50:
                    destination = self._vehicle_destination.get(veh_id, "")
                    if not destination:
                        continue

                    request_time = time.time()
                    self._producer.send(TOPIC_ROUTE_REQUESTS, key=veh_id.encode(), value={
                        "vehicle_id": veh_id,
                        "current_edge": current_edge,
                        "destination": destination,
                        "request_time": request_time,
                    })
                    self._total_requests_sent += 1
                    messages_sent = True
                    if veh_id in self._pending_requests:
                        self._duplicate_requests += 1
                        continue
                    self._vehicle_request_status[veh_id] = current_edge
                    self._pending_requests[veh_id] = request_time

            except traci.TraCIException:
                pass

        if messages_sent:
            self._producer.flush()

    def detect_and_send_edge_transitions(self, vehicle_data: dict) -> None:
        """Detect edge changes and publishes edge_transitions events"""
        sumo_time = traci.simulation.getTime()
        messages_sent = False

        for veh_id, data in vehicle_data.items():
            try:
                current_edge = data[tc.VAR_ROAD_ID]

                # Skip internal edges
                if not current_edge or current_edge.startswith(":"):
                    continue

                # If vehicles last edge is unknown, set it to current edge and continue
                prev = self._vehicle_last_edge.get(veh_id)
                if prev is None:
                    self._vehicle_last_edge[veh_id] = (current_edge, sumo_time)
                    continue

                # If vehicles last edge is the same as current edge, continue
                prev_edge, entry_time = prev
                if current_edge == prev_edge:
                    continue

                # If vehicles destination is unknown, set it to current edge and continue
                dest_edge = self._vehicle_destination.get(veh_id, "")
                if not dest_edge:
                    self._vehicle_last_edge[veh_id] = (current_edge, sumo_time)
                    continue

                self._producer.send(TOPIC_EDGE_TRANSITIONS, key=veh_id.encode(), value={
                    "vehicle_id":  veh_id,
                    "prev_edge":   prev_edge,
                    "new_edge":    current_edge,
                    "travel_time": sumo_time - entry_time,
                    "dest_edge":   dest_edge,
                    "sim_time":    sumo_time,
                })
                messages_sent = True
                self._vehicle_last_edge[veh_id] = (current_edge, sumo_time)

            except traci.TraCIException:
                pass

        if messages_sent:
            self._producer.flush()

    def _route_tail(self, next_edge: str, dest_edge: str) -> tuple[str, ...]:
        """
        Return the cached SUMO-Dijkstra path from next_edge to dest_edge.

        Used to extend setRoute beyond the algorithm decided next edge, so that next slow/missing responses still have valid fallback
        """
        if not dest_edge or next_edge == dest_edge:
            return ()
        key = (next_edge, dest_edge)
        cached = self._route_tail_cache.get(key)
        if cached is not None:
            return cached
        try:
            stage = traci.simulation.findRoute(next_edge, dest_edge)
            edges = stage.edges if stage else ()
            tail = tuple(edges[1:]) if edges else ()
        except traci.TraCIException:
            tail = ()
        if tail:
            self._route_tail_cache[key] = tail
        return tail

    def receive_responses(self, step: int) -> int:
        """
        Poll route_responses and apply them.
        Returns the number of on-time responses processed this step.
        """
        poll_ms = self._tracker.poll_timeout_ms if self._pending_requests else 0
        messages = self._consumer.poll(timeout_ms=poll_ms)
        current_time = time.time()

        timeout_count = 0
        on_time_count = 0
        too_late_count = 0
        exception_count = 0
        max_latency = 0.0

        for topic_partition, msgs in messages.items():
            for msg in msgs:
                response = msg.value
                veh_id = response["vehicle_id"]
                next_edge = response["next_edge"]
                req_time = response.get("request_time", 0)

                latency = current_time - req_time
                if latency > max_latency:
                    max_latency = latency

                self._pending_requests.pop(veh_id, None)

                if latency > MAX_LATENCY_THRESHOLD:
                    timeout_count += 1
                    continue

                try:
                    if veh_id not in traci.vehicle.getIDList():
                        self._responses_dropped_vehicle_gone += 1
                        continue

                    actual_edge = traci.vehicle.getRoadID(veh_id)
                    expected_edge = response.get("current_edge", "")
                    in_junction = actual_edge.startswith(":")

                    if actual_edge == next_edge:
                        # Vehicle already in the correct edge, skip
                        self._responses_already_at_edge += 1
                        continue

                    if expected_edge:
                        if not in_junction and actual_edge != expected_edge:
                            # Vehicle moved to a different edge, response is stale, skip
                            too_late_count += 1
                            continue
                        if in_junction:
                            if self._vehicle_request_status.get(veh_id) != expected_edge:
                                # Vehicle is in a junction from a different request cycle, skip
                                too_late_count += 1
                                continue
                            last_edge, _ = self._vehicle_last_edge.get(veh_id, (None, None))
                            if last_edge != expected_edge:
                                # Vehicle has moved past expected_edge without triggering a new request, response is stale, skip
                                too_late_count += 1
                                continue

                    on_time_count += 1
                    if self._metrics:
                        self._metrics.record_round_trip_latency(latency)
                    try:
                        dest_edge = self._vehicle_destination.get(veh_id, "")
                        tail = self._route_tail(next_edge, dest_edge)
                        if in_junction:
                            traci.vehicle.setRoute(veh_id, [expected_edge, next_edge, *tail])
                        else:
                            traci.vehicle.setRoute(veh_id, [actual_edge, next_edge, *tail])
                    except traci.TraCIException as e:
                        on_time_count -= 1
                        too_late_count += 1
                        exception_count += 1
                        msg = str(e)
                        if "No connection" in msg:
                            self._route_no_connection_count += 1
                            n = self._route_no_connection_count
                            if n % _ROUTE_WARN_BATCH == 1:
                                logger.warning("route_no_connection x%d: %s", n, msg)
                        elif "junction-internal" in msg:
                            self._route_junction_internal_count += 1
                            n = self._route_junction_internal_count
                            if n % _ROUTE_WARN_BATCH == 1:
                                logger.warning("route_junction_internal x%d: %s", n, msg)
                        else:
                            self._route_other_error_count += 1
                            logger.warning("route_replace_failed x%d: %s", self._route_other_error_count, msg)
                except traci.TraCIException:
                    pass

        self._tracker.record(on_time_count, timeout_count, too_late_count, exception_count)

        if timeout_count > 0:
            print(
                f"[Step {step}] Latency warning: "
                f"{timeout_count} timed-out, {on_time_count} on-time | "
                f"max latency: {max_latency:.2f}s | "
                f"pending: {len(self._pending_requests)} | "
                f"poll was: {poll_ms}ms"
            )

        return on_time_count

    def poll_teleports(self) -> None:
        self._teleports_started += traci.simulation.getStartingTeleportNumber()
        self._teleports_ended += traci.simulation.getEndingTeleportNumber()

    def check_arrival(self, veh_id: str) -> None:
        """
        Called for each arrived vehicle, if its last edge doesn't match cached destination, count it as premature arrival
        """
        last = self._vehicle_last_edge.get(veh_id)
        dest = self._vehicle_destination.get(veh_id)
        if last is not None and dest is not None and last[0] != dest:
            self._premature_arrivals += 1

    def cleanup_pending(self, active_vehicles: set) -> None:
        """Remove departed vehicles from all per vehicle tracking dicts"""
        for tracking in (
            self._pending_requests,
            self._vehicle_request_status,
            self._vehicle_destination,
            self._vehicle_last_edge,
        ):
            for v in [v for v in tracking if v not in active_vehicles]:
                del tracking[v]

    def update_throttle(self) -> None:
        """Recompute adaptive poll timeout and step delay from recent timeout rate"""
        self._tracker.update_throttle()

    def set_training_pressure(self, poll_count: int, cap: int) -> None:
        """Forward learner backpressure to the latency tracker"""
        self._tracker.set_training_pressure(poll_count, cap)

    def wait(self) -> None:
        """Sleep for the current adaptive step delay (initially 0)"""
        self._tracker.wait()

    def throttle_stats(self) -> dict:
        return self._tracker.throttle_stats()

    def routing_stats(self) -> dict:
        stats = self._tracker.stats()
        stats["requests_sent"] = self._total_requests_sent
        stats["duplicate_requests"] = self._duplicate_requests
        stats["dropped_vehicle_gone"] = self._responses_dropped_vehicle_gone
        stats["already_at_edge"] = self._responses_already_at_edge
        stats["truly_unaccounted"] = (
            self._total_requests_sent
            - stats["total"]
            - self._responses_dropped_vehicle_gone
            - self._responses_already_at_edge
        )
        stats["route_no_connection"] = self._route_no_connection_count
        stats["route_junction_internal"] = self._route_junction_internal_count
        stats["route_other_error"] = self._route_other_error_count
        stats["teleports_started"] = self._teleports_started
        stats["teleports_ended"] = self._teleports_ended
        stats["premature_arrivals"] = self._premature_arrivals
        return stats

    def summary_line(self) -> str:
        s = self._tracker.summary_line(
            self._total_requests_sent,
            self._responses_dropped_vehicle_gone,
            self._duplicate_requests,
            self._route_no_connection_count,
            self._route_junction_internal_count,
            self._route_other_error_count,
            self._responses_already_at_edge,
        )
        if self._teleports_started:
            s += f" | teleports(start:{self._teleports_started}, end:{self._teleports_ended})"
        if self._premature_arrivals:
            s += f" | premature_arrivals: {self._premature_arrivals}"
        return s
