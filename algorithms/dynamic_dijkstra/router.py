import networkx as nx

from algorithms.base import Router
from common.networking.network import EdgeMap, ConnectionMap
from common.networking.dynamic_dist import DynamicDist

_FALLBACK_SPEED = 14.0


class DynamicDijkstraRouter(Router):
    """Shortest path under the current speeds, replans at every junction"""

    def __init__(self, line_graph: nx.DiGraph, edge_map: EdgeMap, connection_map: ConnectionMap, hp: dict):
        self._connection_map = connection_map
        self._ordered_edges: list[str] = list(edge_map)
        self._idx = {e: i for i, e in enumerate(self._ordered_edges)}
        self._freeflow = [
            (edge_map[e][2] / edge_map[e][3]) if edge_map[e][3] > 0 else _FALLBACK_SPEED
            for e in self._ordered_edges
        ]
        self._dyn = DynamicDist(self._ordered_edges, edge_map, connection_map,
                                max(sum(edge_map[e][3] for e in self._ordered_edges), 1.0), hp["min_ratio"])
        self._unreachable = self._dyn._unreachable
        self.update_traffic_state({})

    def update_traffic_state(self, snapshot: dict[str, dict]):
        """Snapshot format: {edge_id: {"speed": float, ...}}"""
        _E: dict = {}
        speeds = [snapshot.get(e, _E).get("speed", ff) for e, ff in zip(self._ordered_edges, self._freeflow)]
        self._snap = (speeds, self._dyn.edge_times(speeds), {})

    def find_next_edge(self, current_edge: str, dest_edge: str, vehicle_id: str = "") -> str | None:
        if current_edge not in self._idx or dest_edge not in self._idx or current_edge == dest_edge:
            return None
        cands = [c for c in self._connection_map.get(current_edge, []) if c in self._idx]
        if not cands:
            return None
        speeds, times, dist_cache = self._snap
        d = dist_cache.get(dest_edge)
        if d is None:
            d = dist_cache[dest_edge] = self._dyn.seconds(speeds, dest_edge)
        idx = self._idx
        reachable = [c for c in cands if d[idx[c]] < self._unreachable]
        if not reachable:
            return None
        return min(reachable, key=lambda c: times[idx[c]] + d[idx[c]])
