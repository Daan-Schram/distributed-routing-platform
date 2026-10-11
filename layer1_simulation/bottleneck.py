"""
Layer 1: creating a bottleneck in the network.

Slows the edges with the most paths in the routing file. Dijkstra weights on the
.net.xml and setMaxSpeed is runtime-only, so static routing keeps feeding the
bottleneck while Deep Q sees it via the speed feature.
"""
import collections
import xml.etree.ElementTree as ET


def edge_usage(route_file: str) -> collections.Counter:
    usage: collections.Counter = collections.Counter()
    try:
        root = ET.parse(route_file).getroot()
    except (ET.ParseError, OSError) as e:
        print(f"[Bottleneck] Cannot read {route_file}: {e}")
        return usage

    for veh in root.iter("vehicle"):
        route = veh.find("route")
        if route is None or not route.get("edges"):
            continue
        for edge in route.get("edges").split()[:-1]:
            usage[edge] += 1
    return usage


def apply_jam(edges: list[str], factor: float) -> list[str]:
    import traci

    if factor <= 1.0:
        print(f"[Bottleneck] Ignoring jam factor {factor} — must be > 1.0")
        return []
    applied = []
    for edge in edges:
        try:
            freeflow = traci.lane.getMaxSpeed(f"{edge}_0")
            traci.edge.setMaxSpeed(edge, max(freeflow / factor, 0.1))
            applied.append(edge)
        except traci.TraCIException as e:
            print(f"[Bottleneck] Could not slow {edge}: {e}")
    return applied


class JamTracker:
    def __init__(self, edges: list[str]):
        import traci.constants as tc

        self._road_var = tc.VAR_ROAD_ID
        self._edges = set(edges)
        self._veh_steps = 0
        self._jam_veh_steps = 0
        self._touched: set[str] = set()
        self._seen: set[str] = set()
        self._arrived = 0
        self._arrived_touched = 0

    def observe(self, vehicle_data: dict) -> None:
        for veh_id, data in vehicle_data.items():
            self._veh_steps += 1
            self._seen.add(veh_id)
            if data.get(self._road_var) in self._edges:
                self._jam_veh_steps += 1
                self._touched.add(veh_id)

    def on_arrived(self, veh_id: str) -> None:
        self._arrived += 1
        if veh_id in self._touched:
            self._arrived_touched += 1

    def stats(self) -> dict:
        return {
            "veh_time_share": round(self._jam_veh_steps / self._veh_steps, 4) if self._veh_steps else 0.0,
            "arrivals_touching_pct": round(100.0 * self._arrived_touched / self._arrived, 1) if self._arrived else 0.0,
            "vehicles_touching": len(self._touched),
            "vehicles_seen": len(self._seen),
            "arrivals_counted": self._arrived,
        }
