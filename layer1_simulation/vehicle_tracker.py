import traci.constants as tc

from common.config import CONGESTION_SPEED_THRESHOLD
from layer1_simulation.metrics import MetricsStore


class VehicleTracker:
    def __init__(self, metrics: MetricsStore):
        self._metrics = metrics
        self._departure_times: dict[str, float] = {}  # vehicle_id -> sim departure time
        self._last_distance: dict[str, float] = {}    # vehicle_id -> last known VAR_DISTANCE

    def on_departed(self, veh_id: str, sim_time: float) -> None:
        self._departure_times[veh_id] = sim_time
        self._metrics.record_departure()

    def on_arrived(self, veh_id: str, sim_time: float) -> None:
        dep_time = self._departure_times.pop(veh_id, None)
        if dep_time is not None:
            travel_time = sim_time - dep_time
            distance_m = self._last_distance.pop(veh_id, 0.0)
            self._metrics.record_att(travel_time, arrival_sim_time=sim_time)
            self._metrics.record_trip(travel_time=travel_time, distance_m=distance_m)
        else:
            self._last_distance.pop(veh_id, None)

    def process_step(self, vehicle_data: dict, sim_time: float) -> None:
        """Accumulate congestion seconds for this step (SUMO step = 1 s) and cache distances"""
        congestion_vs = 0.0
        for veh_id, data in vehicle_data.items():
            speed = data.get(tc.VAR_SPEED)
            if speed is not None and speed < CONGESTION_SPEED_THRESHOLD:
                congestion_vs += 1.0
            dist = data.get(tc.VAR_DISTANCE)
            if dist is not None:
                self._last_distance[veh_id] = dist
        if congestion_vs > 0.0:
            self._metrics.record_congestion(congestion_vs)
