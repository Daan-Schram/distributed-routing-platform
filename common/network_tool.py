"""
network_tool.py — SUMO network preparation utility

PARAMETERS

--duration  INT   Total simulated time in seconds (single-phase only)
                  Example: 600 = 10 minutes of vehicle departures

--period    FLOAT Mean gap (seconds) between consecutive vehicle departures (single-phase only)
                  Total vehicles generated ~ duration / period
                  Lower period → more vehicles → heavier traffic → gridlock risk
                  Practical range: 0.5 (very dense) to 5.0 (light)

--highway-types STR  Comma-separated OSM highway values to keep (Mode A only), each with its _link variant.
                  Example: "primary,secondary,tertiary,residential,unclassified"
                  Default: osmnx network_type "drive".

--fringe-factor FLOAT  Passed to randomTrips: boost trips that start and end at the network fringe.

--seed      INT   Passed to randomTrips for repeatable trips (phase i uses seed + i).

--phases    STR   Comma-separated list of duration:period pairs for staged traffic.
                  Overrides --duration and --period.
                  Example: "1500:3.0,300:1.5" → 1500s at light load, then 300s of congestion.
                  Each phase runs back-to-back; trips are merged and re-routed in one pass.

USAGE MODES

Mode A — Full pipeline (OSM download → netconvert → randomTrips):
    python common/network_tool.py --name NAME --address "ADDRESS" --dist 1500 --duration 600 --period 3.0
    python common/network_tool.py --name NAME --lat 52.246 --lon 6.851 --dist 1500 --duration 600 --period 3.0

Mode B — Routes only (skip download/netconvert, use existing .net.xml):
    python common/network_tool.py --name NAME --net-file data/networks/NAME.net.xml --duration 600 --period 3.0

Mode B with phases:
    python common/network_tool.py --name NAME --net-file data/networks/NAME.net.xml --phases "1500:3.0,300:1.5"

Mode A keeps only the largest strongly connected component of the edge graph (SUMO connections),
so every destination is reachable from every edge.

In both modes a JSON meta file is written to:
    common/data/networks/{name}.meta.json

Outputs (Mode A only):
    common/data/osm/{name}.osm
    common/data/networks/{name}.net.xml

Outputs (both modes):
    common/data/routes/{name}.trips.xml
    common/data/routes/{name}.rou.xml
    common/data/networks/{name}.meta.json
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
DATA_DIR = PROJECT_ROOT / "common" / "data"
OSM_DIR = DATA_DIR / "osm"
NETWORKS_DIR = DATA_DIR / "networks"
ROUTES_DIR = DATA_DIR / "routes"


def _find_sumo_home() -> Path:
    sumo_home = os.environ.get("SUMO_HOME")
    if sumo_home:
        p = Path(sumo_home)
        if p.is_dir():
            return p
    netconvert = shutil.which("netconvert")
    if netconvert:
        return Path(netconvert).parent.parent
    raise EnvironmentError(
        "SUMO not found. Set SUMO_HOME or ensure netconvert is on PATH."
    )


def _parse_phases(phases_str: str) -> list[tuple[int, float]]:
    """Parse '1500:3.0,300:1.5' → [(1500, 3.0), (300, 1.5)]"""
    phases = []
    for token in phases_str.split(","):
        token = token.strip()
        parts = token.split(":")
        if len(parts) != 2:
            sys.exit(f"Invalid phase token '{token}': expected 'duration:period' (e.g. '1500:3.0')")
        try:
            phases.append((int(parts[0]), float(parts[1])))
        except ValueError:
            sys.exit(f"Invalid phase token '{token}': duration must be int, period must be float")
    return phases


def _generate_phased_trips(
    phases: list[tuple[int, float]],
    net_file: Path,
    trips_file: Path,
    rou_file: Path,
    random_trips_py: Path,
    sumo_home: Path,
    trip_args: list[str],
    seed: int | None,
) -> None:
    """Run randomTrips per phase, merge trips XML, route with duarouter."""
    import xml.etree.ElementTree as ET

    tmp_trips_files: list[Path] = []
    tmp_rou_files: list[Path] = []
    current_begin = 0

    for i, (duration, period) in enumerate(phases):
        end = current_begin + duration
        tmp_trips = trips_file.parent / f"_tmp_phase_{i}.trips.xml"
        tmp_rou   = rou_file.parent   / f"_tmp_phase_{i}.rou.xml"
        tmp_trips_files.append(tmp_trips)
        tmp_rou_files.append(tmp_rou)

        _run([
            sys.executable, str(random_trips_py),
            "-n", str(net_file),
            "-o", str(tmp_trips),
            "-r", str(tmp_rou),
            "-b", str(current_begin),
            "-e", str(end),
            "-p", str(period),
            *trip_args,
            *(["--seed", str(seed + i)] if seed is not None else []),
        ], label=f"randomTrips phase {i + 1}/{len(phases)} (t={current_begin}s–{end}s, period={period}s)")
        current_begin = end

    print("\n[merge] Merging trip phases...")
    all_trips: list = []
    for tmp in tmp_trips_files:
        tree = ET.parse(tmp)
        all_trips.extend(tree.getroot().findall("trip"))

    all_trips.sort(key=lambda t: float(t.get("depart", 0)))
    for i, trip in enumerate(all_trips):
        trip.set("id", str(i))

    root = ET.Element("routes", {
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "xsi:noNamespaceSchemaLocation": "http://sumo.dlr.de/xsd/routes_file.xsd",
    })
    for trip in all_trips:
        root.append(trip)
    merged_tree = ET.ElementTree(root)
    ET.indent(merged_tree, space="    ")
    merged_tree.write(str(trips_file), encoding="UTF-8", xml_declaration=True)
    print(f"[merge] Wrote {len(all_trips)} trips to {trips_file}")

    # Run duarouter once on the merged trips to generate the final rou.xml
    duarouter = sumo_home / "bin" / "duarouter"
    if sys.platform == "win32" and not duarouter.is_file():
        duarouter = sumo_home / "bin" / "duarouter.exe"
    if not Path(str(duarouter)).is_file():
        found = shutil.which("duarouter")
        duarouter = Path(found) if found else Path("duarouter")

    _run([
        str(duarouter),
        "--net-file", str(net_file),
        "--route-files", str(trips_file),
        "--output-file", str(rou_file),
        "--ignore-errors",
    ], label="duarouter")

    for f in tmp_trips_files + tmp_rou_files:
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    print("[merge] Cleaned up temporary phase files.")


def _keep_largest_scc(net_file: Path) -> int:
    """Rebuild the net with only its largest best connected edge component"""
    import networkx as nx
    from common.networking.network import load_network

    for _ in range(5):
        _, line_graph, edge_map, _ = load_network(str(net_file))
        largest = max(nx.strongly_connected_components(line_graph), key=len)
        if len(largest) == len(edge_map):
            print(f"[scc] All {len(edge_map)} edges are strongly connected.")
            return len(edge_map)
        print(f"[scc] Keeping {len(largest)} of {len(edge_map)} edges.")
        keep_file = net_file.with_name(net_file.name + ".keep.txt")
        tmp_net = net_file.with_name(net_file.name + ".tmp.xml")
        keep_file.write_text("\n".join(sorted(largest)) + "\n", encoding="utf-8")
        _run([
            "netconvert",
            "--sumo-net-file", str(net_file),
            "--keep-edges.input-file", str(keep_file),
            "-o", str(tmp_net),
        ], label="netconvert scc")
        os.replace(tmp_net, net_file)
        keep_file.unlink()
    sys.exit("[scc] Network still not strongly connected after 5 passes.")


def _run(cmd: list[str], label: str) -> None:
    print(f"\n[{label}] Running: {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        sys.exit(f"[{label}] failed with exit code {result.returncode}")
    print(f"[{label}] Done.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SUMO network preparation utility. See module docstring for full usage."
    )
    parser.add_argument("--name", required=True, help="Network name (used as filename stem)")
    parser.add_argument(
        "--net-file",
        metavar="NET_FILE",
        help="Path to an existing .net.xml. If given, skip OSM download and netconvert (Mode B).",
    )
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--address", help="Geocodable address string (Mode A only)")
    location.add_argument("--lat", type=float, help="Latitude (Mode A only)")
    parser.add_argument("--lon", type=float, help="Longitude, required with --lat (Mode A only)")
    parser.add_argument("--dist", type=int, default=1500, help="Radius in metres for OSM download")
    parser.add_argument(
        "--duration", type=int, default=600,
        help="Simulation duration in seconds; trips spread across this window (default: 600). Ignored when --phases is set.",
    )
    parser.add_argument(
        "--period", type=float, default=1.0,
        help="Mean inter-departure gap in seconds; total vehicles ~ duration/period (default: 1.0). Ignored when --phases is set.",
    )
    parser.add_argument(
        "--highway-types",
        help='OSM highway values to keep (Mode A only), e.g. "primary,secondary,tertiary,residential,unclassified".',
    )
    parser.add_argument("--fringe-factor", type=float, help="randomTrips --fringe-factor")
    parser.add_argument("--seed", type=int, help="randomTrips --seed (phase i uses seed + i)")
    parser.add_argument(
        "--phases",
        help='Staged traffic: comma-separated duration:period pairs, e.g. "1500:3.0,300:1.5". '
             'Overrides --duration and --period.',
    )
    args = parser.parse_args()

    if args.net_file is None:
        if args.address is None and args.lat is None:
            parser.error("Either --net-file or one of --address / --lat+--lon is required.")
        if args.lat is not None and args.lon is None:
            parser.error("--lon is required when using --lat.")

    OSM_DIR.mkdir(parents=True, exist_ok=True)
    NETWORKS_DIR.mkdir(parents=True, exist_ok=True)
    ROUTES_DIR.mkdir(parents=True, exist_ok=True)

    osm_file   = OSM_DIR      / f"{args.name}.osm"
    net_file   = NETWORKS_DIR / f"{args.name}.net.xml"
    trips_file = ROUTES_DIR   / f"{args.name}.trips.xml"
    rou_file   = ROUTES_DIR   / f"{args.name}.rou.xml"
    meta_file  = NETWORKS_DIR / f"{args.name}.meta.json"

    if args.net_file is None:
        # ── Mode A: full pipeline ─────────────────────────────────────────────
        try:
            import osmnx as ox
        except ImportError:
            sys.exit("osmnx is not installed. Run: pip install osmnx")

        ox.settings.all_oneway = True

        if args.address:
            print(f"\n[osmnx] Geocoding '{args.address}'...")
            center = ox.geocode(args.address)
            print(f"[osmnx] Resolved to lat={center[0]:.4f}, lon={center[1]:.4f}")
        else:
            center = (args.lat, args.lon)
            print(f"\n[osmnx] Using center lat={center[0]:.4f}, lon={center[1]:.4f}")

        print(f"[osmnx] Downloading drive network within {args.dist} m...")
        if args.highway_types:
            types = "|".join(t.strip() for t in args.highway_types.split(",") if t.strip())
            custom_filter = f'["highway"~"^({types})(_link)?$"]["access"!~"private|no"]["motor_vehicle"!~"private|no"]'
            print(f"[osmnx] Filter: {custom_filter}")
            G = ox.graph_from_point(center, dist=args.dist, custom_filter=custom_filter, simplify=False)
        else:
            G = ox.graph_from_point(center, dist=args.dist, network_type="drive", simplify=False)
        ox.save_graph_xml(G, filepath=str(osm_file))
        print(f"[osmnx] Saved OSM to {osm_file}")

        _run([
            "netconvert",
            "--osm-files", str(osm_file),
            "-o", str(net_file),
            "--geometry.remove",
            "--ramps.guess",
            "--junctions.join",
            "--tls.guess-signals",
            "--keep-edges.by-vclass", "passenger",
            "--remove-edges.isolated",
            "--no-turnarounds.except-deadend",
        ], label="netconvert")
        edge_count = _keep_largest_scc(net_file)

    else:
        # ── Mode B: routes only ───────────────────────────────────────────────
        src = Path(args.net_file)
        if not src.is_file():
            sys.exit(f"--net-file '{src}' does not exist.")
        if src.resolve() != net_file.resolve():
            shutil.copy2(str(src), str(net_file))
            print(f"[network_tool] Copied {src} → {net_file}")
        else:
            print(f"[network_tool] Using existing net file at {net_file}")

    sumo_home = _find_sumo_home()
    random_trips_py = sumo_home / "tools" / "randomTrips.py"
    if not random_trips_py.is_file():
        sys.exit(f"randomTrips.py not found at {random_trips_py}. Check SUMO_HOME.")

    trip_args = ["--fringe-factor", str(args.fringe_factor)] if args.fringe_factor is not None else []
    if args.phases:
        phases = _parse_phases(args.phases)
        _generate_phased_trips(
            phases, net_file, trips_file, rou_file, random_trips_py, sumo_home, trip_args, args.seed
        )
        total_duration = sum(d for d, _ in phases)
        meta = {
            "duration": total_duration,
            "period": None,
            "phases": [{"duration": d, "period": p} for d, p in phases],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        _run([
            sys.executable, str(random_trips_py),
            "-n", str(net_file),
            "-o", str(trips_file),
            "-r", str(rou_file),
            "-e", str(args.duration),
            "-p", str(args.period),
            *trip_args,
            *(["--seed", str(args.seed)] if args.seed is not None else []),
        ], label="randomTrips")
        meta = {
            "duration": args.duration,
            "period": args.period,
            "phases": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    meta.update({
        "fringe_factor": args.fringe_factor,
        "seed": args.seed,
    })
    if args.net_file is None:
        meta.update({
            "center": [center[0], center[1]],
            "dist": args.dist,
            "highway_types": args.highway_types,
            "edges": edge_count,
        })
    with open(meta_file, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"[network_tool] Metadata written to {meta_file}")

    print(f"\nNetwork '{args.name}' ready.")
    print(f"  Network : {net_file}")
    print(f"  Routes  : {rou_file}")
    print(f"  Metadata: {meta_file}")
    print(f"\nSelect '{args.name}' in the experiment dashboard to use this network.")


if __name__ == "__main__":
    main()
