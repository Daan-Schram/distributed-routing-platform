"""
Layer 1 — vehicle trajectory capture (for debug / route-loop visualization)
"""
import json
from pathlib import Path

import networkx as nx
import traci.constants as tc


class TrajectoryRecorder:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._paths: dict[str, list[str]] = {}   # vehicle_id -> ordered edge list
        self._last_edge: dict[str, str] = {}      # vehicle_id -> last appended edge
        self._dest: dict[str, str] = {}           # vehicle_id -> intended destination edge
        self._hops_cache: dict[str, dict] = {}    # vehicle_id -> Dijkstra hop comparison

    def record_destination(self, veh_id: str, dest_edge: str) -> None:
        """Store a vehicle's intended final edge (from its SUMO route) at departure."""
        if not self.enabled or not dest_edge or dest_edge.startswith(":"):
            return
        self._dest[veh_id] = dest_edge

    def record_step(self, vehicle_data: dict) -> None:
        """Append the current edge for each vehicle when it changes (call every step)."""
        if not self.enabled:
            return
        for veh_id, data in vehicle_data.items():
            edge = data.get(tc.VAR_ROAD_ID)
            if not edge or edge.startswith(":"):
                continue
            if self._last_edge.get(veh_id) == edge:
                continue
            self._last_edge[veh_id] = edge
            self._paths.setdefault(veh_id, []).append(edge)

    def loop_stats(self, line_graph: "nx.DiGraph | None" = None) -> dict | None:
        """Summarize looping across all captured vehicles"""
        if not self.enabled or not self._paths:
            return None
        total = len(self._paths)
        revisits = []
        looped = 0
        for path in self._paths.values():
            rv = len(path) - len(set(path))
            revisits.append(rv)
            if rv > 0:
                looped += 1
        stats = {
            "vehicles_total": total,
            "vehicles_with_loops": looped,
            "looped_pct": round(100.0 * looped / total, 2) if total else 0.0,
            "mean_revisits": round(sum(revisits) / total, 3) if total else 0.0,
            "max_revisits": max(revisits) if revisits else 0,
        }
        stats.update(self._hops_stats(line_graph))
        return stats

    def _hops_stats(self, line_graph: "nx.DiGraph | None") -> dict:
        if line_graph is None:
            return {"hops_ratio": None, "mean_extra_edges": None, "hops_compared": 0}
        ratios, extras = [], []
        for veh_id, path in self._paths.items():
            m = self._vehicle_hops(veh_id, path, line_graph)
            if m["dijkstra_edges"]:
                ratios.append(len(path) / m["dijkstra_edges"])
                extras.append(m["extra_edges"])
        if not ratios:
            return {"hops_ratio": None, "mean_extra_edges": None, "hops_compared": 0}
        return {
            "hops_ratio": round(sum(ratios) / len(ratios), 3),
            "mean_extra_edges": round(sum(extras) / len(extras), 2),
            "hops_compared": len(ratios),
        }

    def _vehicle_hops(self, veh_id: str, path: list[str], line_graph: "nx.DiGraph | None") -> dict:
        if veh_id in self._hops_cache:
            return self._hops_cache[veh_id]
        dijkstra_edges = None
        extra_edges = None
        start = path[0] if path else None
        dest = self._dest.get(veh_id)
        if line_graph is not None and start and dest and start in line_graph and dest in line_graph:
            try:
                sp = nx.shortest_path(line_graph, start, dest, weight="time")
                dijkstra_edges = len(sp)
                extra_edges = len(path) - dijkstra_edges
            except nx.NetworkXNoPath:
                pass
        result = {"dijkstra_edges": dijkstra_edges, "extra_edges": extra_edges}
        self._hops_cache[veh_id] = result
        return result

    def _vehicle_meta(self, line_graph: "nx.DiGraph | None") -> dict:
        meta: dict[str, dict] = {}
        for veh_id, path in self._paths.items():
            hops = self._vehicle_hops(veh_id, path, line_graph)
            meta[veh_id] = {
                "revisits": len(path) - len(set(path)),
                "dijkstra_edges": hops["dijkstra_edges"],
                "extra_edges": hops["extra_edges"],
                "dest_edge": self._dest.get(veh_id),
            }
        return meta

    def write(self, out_dir: str, run_id: str, network_file: str,
              line_graph: "nx.DiGraph | None" = None) -> None:
        """Write the full per-vehicle trajectories to <out_dir>/<run_id>.json."""
        if not self.enabled or not self._paths:
            return
        path = Path(out_dir)
        path.mkdir(parents=True, exist_ok=True)
        out_file = path / f"{run_id}.json"
        payload = {
            "run_id": run_id,
            "network_file": network_file,
            "loops": self.loop_stats(line_graph),
            "meta": self._vehicle_meta(line_graph),
            "vehicles": self._paths,
        }
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        print(f"[Trajectories] {len(self._paths)} vehicles written to {out_file}")
