"""
Jam calibration, Dijkstra SUMO run over (JAM_TOP_EDGES, JAM_FACTOR), to see what is difficult for Dijkstra but not impossible
python experiments/jam_calibration.py --network irregular_6x6_staged_medium --edges 2,3,4 --factors 3,4,6,8,12
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import network_file_path, route_file_path
from layer1_simulation.bottleneck import edge_usage, apply_jam
from layer1_simulation.run_simulation import get_sumo_binary, get_sumo_tools_path

sys.path.append(get_sumo_tools_path())
import traci  # noqa: E402


def select_edges(route_file: str, n: int) -> tuple[list[str], float]:
    usage = dict(edge_usage(route_file))
    total = sum(usage.values())
    edges = [e for e, _ in sorted(usage.items(), key=lambda kv: -kv[1])[:n]]
    share = sum(usage[e] for e in edges) / total if total else 0.0
    return edges, share


def run_once(net: str, rou: str, n_edges: int, factor: float, max_time: float, label: str) -> dict:
    cmd = [get_sumo_binary(False), "-n", net, "-r", rou,
           "--no-step-log", "true", "--waiting-time-memory", "1000",
           "--max-depart-delay", "30", "--no-warnings", "true"]
    traci.start(cmd, label=label)
    conn = traci.getConnection(label)
    edges, share = ([], 0.0)
    if n_edges > 0:
        edges, share = select_edges(rou, n_edges)
        edges = apply_jam(edges, factor)
    jam = set(edges)

    depart: dict[str, float] = {}
    touched: set[str] = set()
    arrived: list[float] = []
    arrived_touched = 0
    t0 = time.time()
    while conn.simulation.getMinExpectedNumber() > 0:
        conn.simulationStep()
        now = conn.simulation.getTime()
        for v in conn.simulation.getDepartedIDList():
            depart[v] = now
        if jam:
            for v in conn.vehicle.getIDList():
                if conn.vehicle.getRoadID(v) in jam:
                    touched.add(v)
        for v in conn.simulation.getArrivedIDList():
            if v in depart:
                arrived.append(now - depart[v])
                arrived_touched += v in touched
        if now >= max_time:
            break
    teleports = conn.simulation.getStartingTeleportNumber()
    sim_time = conn.simulation.getTime()
    conn.close()

    n_arr = len(arrived)
    att = sum(arrived) / n_arr if n_arr else float("nan")
    not_arrived = len(depart) - n_arr
    return {
        "edges": edges, "factor": factor if edges else None, "share": round(share, 4),
        "departures": len(depart), "arrived": n_arr, "not_arrived": not_arrived,
        "att": round(att, 1), "p95": round(sorted(arrived)[int(0.95 * n_arr)], 1) if n_arr else None,
        "teleports": teleports, "sim_time": sim_time,
        "arrivals_touching_pct": round(100.0 * arrived_touched / n_arr, 1) if n_arr and jam else None,
        "wall_s": round(time.time() - t0, 1),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--network", default="irregular_6x6_staged_medium")
    p.add_argument("--edges", default="2,3,4")
    p.add_argument("--factors", default="3,4,6,8,12")
    p.add_argument("--max-time", type=float, default=20000)
    p.add_argument("--out", default="output/jam_calibration")
    a = p.parse_args()

    net, rou = network_file_path(a.network), route_file_path(a.network)
    os.makedirs(a.out, exist_ok=True)
    out_path = os.path.join(a.out, f"{a.network}.json")
    results = []

    def record(r):
        results.append(r)
        print(f"N={len(r['edges'])} x{r['factor'] or 1:<4g} share={r['share']:.3f} "
              f"ATT={r['att']:7.1f} ratio={r['att'] / results[0]['att']:5.2f}x "
              f"dep={r['departures']}/{total} arr={r['arrived']} tp={r['teleports']:3d} "
              f"touch={r['arrivals_touching_pct']} sim={r['sim_time']:.0f}s wall={r['wall_s']}s", flush=True)
        with open(out_path, "w") as f:
            json.dump({"network": a.network, "vehicles": total, "runs": results}, f, indent=1)

    import xml.etree.ElementTree as ET
    total = sum(1 for _ in ET.parse(rou).getroot().iter("vehicle"))
    record(run_once(net, rou, 0, 1.0, a.max_time, "base"))
    for n in [int(x) for x in a.edges.split(",")]:
        for fac in [float(x) for x in a.factors.split(",")]:
            record(run_once(net, rou, n, fac, a.max_time, f"j{n}_{fac:g}"))
    print(f"written {out_path}")


if __name__ == "__main__":
    main()
