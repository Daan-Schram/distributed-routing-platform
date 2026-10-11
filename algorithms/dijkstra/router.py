import networkx as nx

from algorithms.base import Router
from common.networking.network import EdgeMap, ConnectionMap


class DijkstraRouter(Router):
    """
    Shortest-path router using Dijkstra on a line graph.
    Caches best next edge per (current_edge, dest_edge) pair.
    """

    def __init__(self, line_graph: nx.DiGraph, edge_map: EdgeMap, connection_map: ConnectionMap, hp: dict):
        self._line_graph = line_graph
        self._edge_map = edge_map
        self._connection_map = connection_map
        self._cache: dict[tuple[str, str], str | None] = {}

    def find_next_edge(self, current_edge: str, dest_edge: str, vehicle_id: str = "") -> str | None:
        """Return the next edge ID after current_edge on the shortest path to dest_edge."""
        if current_edge not in self._edge_map or dest_edge not in self._edge_map:
            return None

        if current_edge == dest_edge:
            return None

        # Cache keyed by (current_edge, dest_edge)
        cache_key = (current_edge, dest_edge)
        if cache_key not in self._cache:
            try:
                # Find shortest path in the line graph (edge nodes)
                path = nx.shortest_path(self._line_graph, current_edge, dest_edge, weight="time")
                # path[0] is current_edge, path[1] is the next edge
                self._cache[cache_key] = path[1] if len(path) > 1 else None
            except nx.NetworkXNoPath:
                self._cache[cache_key] = None

        return self._cache[cache_key]
