"""Graph traffic environment
No SUMO, no Kafka, no Faust.

Returns experience tuples in the exact shape `agent_worker` puts on the
`experience_replays` topic, so they feed `DeepQLearner.push()` the same
"""

from __future__ import annotations

import collections
import random
from dataclasses import dataclass, field

import networkx as nx

from common.networking.network import load_network

VEHICLE_SPACING_M = 8.0


@dataclass
class Vehicle:
    vid: str
    start_edge: str
    dest_edge: str
    depart_time: float
    current_edge: str | None = None
    entered_at: float = 0.0
    busy_until: float = 0.0
    hops: int = 0
    travel_time: float = 0.0
    visits: collections.Counter = field(default_factory=collections.Counter)
    arrived: bool = False
    gave_up: bool = False


@dataclass
class EpisodeResult:
    vehicles: int
    arrived: int
    gave_up: int
    mean_travel_time: float
    censored_travel_time: float
    mean_hops: float
    hops_ratio: float
    time_ratio: float
    looped_pct: float
    mean_revisits: float
    transitions: int
    sim_time: float

    @property
    def arrival_rate(self) -> float:
        return self.arrived / self.vehicles if self.vehicles else 0.0

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["arrival_rate"] = round(self.arrival_rate, 4)
        for k in ("mean_travel_time", "censored_travel_time", "mean_hops", "hops_ratio",
                  "time_ratio", "looped_pct", "mean_revisits", "sim_time"):
            d[k] = round(d[k], 4)
        return d


class GraphTrafficEnv:
    """Event-driven micro-simulator over a SUMO network's line-graph."""

    def __init__(
        self,
        net_file: str,
        n_vehicles: int = 100,
        depart_period: float = 5.0,
        max_hops: int = 150,
        congestion_k: float = 0.0,
        jam_edges: dict[str, float] | None = None,
        min_path_hops: int = 3,
        broadcast_interval: float = 50.0,
        blind_traffic: bool = False,
        speed_ema: float = 1.0,
        seed: int = 0,
    ):
        self._graph, self.line_graph, self.edge_map, self.connection_map = load_network(net_file)

        self.ordered_edges: list[str] = list(self.edge_map)
        self._idx = {e: i for i, e in enumerate(self.ordered_edges)}
        self.n_edges = len(self.ordered_edges)

        self._freeflow_speed = [
            (self.edge_map[e][2] / self.edge_map[e][3]) if self.edge_map[e][3] > 0 else 14.0
            for e in self.ordered_edges
        ]
        self._capacity = [
            max(1.0, self.edge_map[e][2] / VEHICLE_SPACING_M) for e in self.ordered_edges
        ]

        self.n_vehicles = n_vehicles
        self.depart_period = depart_period
        self.max_hops = max_hops
        self.congestion_k = congestion_k
        self.jam_edges = jam_edges or {}
        self.min_path_hops = min_path_hops
        self.broadcast_interval = broadcast_interval
        self.blind_traffic = blind_traffic
        self.speed_ema = speed_ema
        self._ema_speed: list[float] | None = None
        self._rng = random.Random(seed)

        self._routable = [e for e in self.ordered_edges if self.connection_map.get(e)]
        self._ref_cache: dict[tuple[str, str], tuple[int, float]] = {}

        self._counts = [0] * self.n_edges
        self.sim_time = 0.0
        self.vehicles: list[Vehicle] = []
        self._active: list[Vehicle] = []
        self._pending: list[Vehicle] = []

    def sample_od_pairs(self, n: int, seed: int | None = None) -> list[tuple[str, str]]:
        rng = random.Random(seed) if seed is not None else self._rng
        pairs: list[tuple[str, str]] = []
        guard = 0
        while len(pairs) < n and guard < n * 200:
            guard += 1
            start = rng.choice(self._routable)
            dest = rng.choice(self.ordered_edges)
            if dest == start:
                continue
            try:
                path = nx.shortest_path(self.line_graph, start, dest, weight="time")
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                continue
            if len(path) - 1 < self.min_path_hops:
                continue
            pairs.append((start, dest))
        if len(pairs) < n:
            raise RuntimeError(f"only found {len(pairs)}/{n} OD pairs with >={self.min_path_hops} hops")
        return pairs

    def load_route_file(self, route_file: str) -> list[tuple[str, str, float]]:
        """(start, dest, depart) of every vehicle in a SUMO .rou.xml, in departing order"""
        import xml.etree.ElementTree as ET
        trips = []
        for veh in ET.parse(route_file).getroot().iter("vehicle"):
            route = veh.find("route")
            edges = route.get("edges", "").split() if route is not None else []
            if len(edges) < 2 or edges[0] not in self._idx or edges[-1] not in self._idx:
                continue
            if not self.connection_map.get(edges[0]):
                continue
            trips.append((edges[0], edges[-1], float(veh.get("depart", 0))))
        trips.sort(key=lambda t: t[2])
        return trips

    def reference_path(self, start: str, dest: str) -> tuple[int, float]:
        key = (start, dest)
        if key not in self._ref_cache:
            path = nx.shortest_path(self.line_graph, start, dest, weight="time")
            t = sum(self.edge_map[e][3] for e in path[:-1])
            self._ref_cache[key] = (len(path) - 1, t)
        return self._ref_cache[key]

    def _edge_speed(self, i: int) -> float:
        load = self._counts[i] / self._capacity[i]
        speed = self._freeflow_speed[i] / (1.0 + self.congestion_k * load)
        jam = self.jam_edges.get(self.ordered_edges[i])
        if jam:
            speed /= jam
        return max(speed, 0.1)

    def _edge_time(self, edge: str) -> float:
        return self.edge_map[edge][2] / self._edge_speed(self._idx[edge])

    def _observed_speed(self, i: int) -> float:
        if self.blind_traffic:
            return self._freeflow_speed[i]
        if self.speed_ema >= 1.0 or self._ema_speed is None:
            return self._edge_speed(i)
        return self._ema_speed[i]

    def _tick_ema(self) -> None:
        if self.speed_ema >= 1.0:
            return
        cur = [self._edge_speed(i) for i in range(self.n_edges)]
        if self._ema_speed is None:
            self._ema_speed = cur
        else:
            a = self.speed_ema
            self._ema_speed = [a * c + (1 - a) * p for c, p in zip(cur, self._ema_speed)]

    def _observed_count(self, i: int) -> float:
        return 0.0 if self.blind_traffic else float(self._counts[i])

    def _snapshot(self) -> dict:
        return {
            "speeds": [self._observed_speed(i) for i in range(self.n_edges)],
            "counts": [self._observed_count(i) for i in range(self.n_edges)],
        }

    def _broadcast_snapshot(self) -> dict:
        return {
            e: {"speed": self._observed_speed(i), "count": self._observed_count(i)}
            for i, e in enumerate(self.ordered_edges)
        }

    def reset(self, od_pairs: list[tuple[str, str]]) -> None:
        self._counts = [0] * self.n_edges
        self._ema_speed = None
        self.sim_time = 0.0
        self.vehicles = []
        for k, od in enumerate(od_pairs):
            start, dest = od[0], od[1]
            depart = od[2] if len(od) > 2 else k * self.depart_period
            v = Vehicle(vid=f"v{k}", start_edge=start, dest_edge=dest, depart_time=depart)
            v.visits = collections.Counter((start,))
            self.vehicles.append(v)
        self._pending = list(self.vehicles)
        self._active = []

    def _depart_due(self) -> None:
        still = []
        for v in self._pending:
            if v.depart_time <= self.sim_time:
                v.current_edge = v.start_edge
                v.entered_at = self.sim_time
                self._counts[self._idx[v.start_edge]] += 1
                v.busy_until = self.sim_time + self._edge_time(v.start_edge)
                self._active.append(v)
            else:
                still.append(v)
        self._pending = still

    def run_episode(self, policy, od_pairs, on_transition=None, on_broadcast=None,
                    deadline: float | None = None) -> EpisodeResult:
        import time as _time
        self.reset(od_pairs)
        transitions = 0
        next_broadcast = 0.0

        while self._pending or self._active:
            if deadline is not None and _time.time() > deadline:
                for v in self._active:
                    v.gave_up = True
                break

            candidates = []
            if self._active:
                candidates.append(min(v.busy_until for v in self._active))
            if self._pending:
                candidates.append(min(v.depart_time for v in self._pending))
            self.sim_time = max(self.sim_time, min(candidates))

            self._depart_due()

            if self.sim_time >= next_broadcast:
                self._tick_ema()
                if on_broadcast is not None:
                    on_broadcast(self._broadcast_snapshot())
                next_broadcast = self.sim_time + self.broadcast_interval

            due = [v for v in self._active if v.busy_until <= self.sim_time]
            for v in due:
                exp = self._hop(v, policy)
                if exp is not None:
                    transitions += 1
                    if on_transition is not None:
                        on_transition(exp)

            for v in due:
                if v.arrived or v.gave_up:
                    self._active.remove(v)

        return self._summarise(transitions)

    def _hop(self, v: Vehicle, policy) -> dict | None:
        prev_edge = v.current_edge

        state_before = self._snapshot()
        action = policy(prev_edge, v.dest_edge, v.vid)

        if action is None or action not in self._idx:
            v.gave_up = True
            self._counts[self._idx[prev_edge]] -= 1
            return None

        travel_time = self.sim_time - v.entered_at
        revisit_count = v.visits[action]

        self._counts[self._idx[prev_edge]] -= 1
        self._counts[self._idx[action]] += 1
        v.current_edge = action
        v.entered_at = self.sim_time
        v.busy_until = self.sim_time + self._edge_time(action)
        v.hops += 1
        v.travel_time += travel_time
        v.visits[action] += 1

        state_after = self._snapshot()

        if action == v.dest_edge:
            v.arrived = True
            self._counts[self._idx[action]] -= 1
        elif v.hops >= self.max_hops:
            v.gave_up = True
            self._counts[self._idx[action]] -= 1

        return {
            "vehicle_id": v.vid,
            "prev_edge": prev_edge,
            "new_edge": action,
            "state_before": state_before,
            "action": action,
            "travel_time": travel_time,
            "state_after": state_after,
            "dest_edge": v.dest_edge,
            "sim_time": self.sim_time,
            "is_revisit": revisit_count > 0,
            "revisit_count": revisit_count,
        }

    def _summarise(self, transitions: int) -> EpisodeResult:
        arrived = [v for v in self.vehicles if v.arrived]
        gave_up = [v for v in self.vehicles if v.gave_up]

        if arrived:
            mean_tt = sum(v.travel_time for v in arrived) / len(arrived)
            mean_hops = sum(v.hops for v in arrived) / len(arrived)
            ratios_h, ratios_t = [], []
            for v in arrived:
                ref_h, ref_t = self.reference_path(v.start_edge, v.dest_edge)
                if ref_h > 0:
                    ratios_h.append(v.hops / ref_h)
                if ref_t > 0:
                    ratios_t.append(v.travel_time / ref_t)
            hops_ratio = sum(ratios_h) / len(ratios_h) if ratios_h else 0.0
            time_ratio = sum(ratios_t) / len(ratios_t) if ratios_t else 0.0
        else:
            mean_tt = mean_hops = hops_ratio = time_ratio = 0.0

        looped, revisits = 0, 0
        for v in self.vehicles:
            extra = sum(v.visits.values()) - len(v.visits)
            revisits += extra
            if extra > 0:
                looped += 1

        # mean_travel_time only sees survivors, so a policy that strands its hard
        # trips scores well on it. Censored counts every vehicle, with a stranded
        # one charged the time it had already grown (a lower bound)
        censored = (sum(v.travel_time for v in self.vehicles) / len(self.vehicles)
                    if self.vehicles else 0.0)

        return EpisodeResult(
            vehicles=len(self.vehicles),
            arrived=len(arrived),
            gave_up=len(gave_up),
            mean_travel_time=mean_tt,
            censored_travel_time=censored,
            mean_hops=mean_hops,
            hops_ratio=hops_ratio,
            time_ratio=time_ratio,
            looped_pct=100.0 * looped / len(self.vehicles) if self.vehicles else 0.0,
            mean_revisits=revisits / len(self.vehicles) if self.vehicles else 0.0,
            transitions=transitions,
            sim_time=self.sim_time,
        )
