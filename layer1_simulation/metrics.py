import json
import statistics
from collections import deque
from datetime import datetime
from pathlib import Path


NUM_DIST_BINS = 10
SCHEMA_VERSION = 2
DECISION_SPLIT_KEYS = ("transport_s", "prep_s", "compute_s")


def _make_trip_distribution(trip_data: list[dict]) -> dict | None:
    """Compute bins for travel_time and distance_m across all trips"""
    if not trip_data:
        return None

    times = [t["travel_time"] for t in trip_data]
    dists = [t["distance_m"] for t in trip_data]

    def _bin(values: list[float]) -> dict:
        lo, hi = min(values), max(values)
        if lo == hi:
            return {"bins": [{"lo": lo, "hi": hi, "count": len(values)}]}
        width = (hi - lo) / NUM_DIST_BINS
        counts = [0] * NUM_DIST_BINS
        for v in values:
            idx = min(int((v - lo) / width), NUM_DIST_BINS - 1)
            counts[idx] += 1
        bins = [{"lo": round(lo + i * width, 2), "hi": round(lo + (i + 1) * width, 2), "count": counts[i]}
                for i in range(NUM_DIST_BINS)]
        return {"bins": bins}

    return {
        "travel_time_s": _bin(times),
        "distance_m": _bin(dists),
        "count": len(trip_data),
    }


def _mean_p95(values: list[float], digits: int = 4) -> dict:
    if not values:
        return {"mean": None, "p95": None, "n": 0}
    s = sorted(values)
    return {"mean": round(statistics.mean(s), digits), "p95": round(s[min(int(len(s) * 0.95), len(s) - 1)], digits),
            "n": len(s)}


def make_run_id(algorithm: str) -> str:
    """Generate a unique run identifier with algorithm and datetime"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{algorithm}_{ts}"


class MetricsStore:
    def __init__(self, run_id: str, algo, output_dir: str, config: dict | None = None, group_id: str = ""):
        self.run_id = run_id
        self._algo = algo
        self.group_id = group_id
        self.output_path = Path(__file__).resolve().parent.parent / output_dir
        self.output_path.mkdir(parents=True, exist_ok=True)
        self._config: dict = config or {}

        # ATT time series, one entry per arrived vehicle
        self._att_series: list[dict] = [] # {arrival_sim_time, att}
        self._arrivals_count: int = 0

        # Departure count
        self._departure_count: int = 0

        # Round-trip latency (Layer 1: request sent → response applied)
        self._round_trip_latencies: list[float] = []

        # Congestion binned by snapshot interval
        self._congestion_bin_acc: float = 0.0 # accumulates between snapshots
        self._congestion_series: list[dict] = [] # {sim_time, vehicle_seconds}

        # Trips completed: cumulative count at each snapshot
        self._trips_series: list[dict] = [] # {sim_time, cumulative_arrivals}

        # From Layer 3 metric events
        self._decision_latencies: list[float] = []
        self._decision_split: dict[str, list[float]] = {k: [] for k in DECISION_SPLIT_KEYS}
        self._edge_travel_times: deque[float] = deque(maxlen=500)

        # From Layer 4 metric events
        self._algorithm_series: list[dict] = []

        # Per-vehicle trip data (travel time + distance)
        self._trip_data: list[dict] = []  # [{travel_time, distance_m}]

        # Resource samples from all layers
        self._resource_samples: dict[str, list[dict]] = {}

    def record_trip(self, travel_time: float, distance_m: float) -> None:
        self._trip_data.append({"travel_time": round(travel_time, 2), "distance_m": round(distance_m, 1)})

    def record_att(self, sim_travel_time_s: float, arrival_sim_time: float) -> None:
        self._att_series.append({"arrival_sim_time": arrival_sim_time, "att": sim_travel_time_s})
        self._arrivals_count += 1

    def record_departure(self) -> None:
        self._departure_count += 1

    def record_round_trip_latency(self, latency_s: float) -> None:
        self._round_trip_latencies.append(latency_s)

    def record_congestion(self, vehicle_seconds: float) -> None:
        self._congestion_bin_acc += vehicle_seconds

    def record_snapshot(self, sim_time: float) -> None:
        """Record a periodic snapshot of congestion and cumulative trips. Call every SNAPSHOT_INTERVAL sim seconds."""
        self._congestion_series.append({"sim_time": sim_time, "vehicle_seconds": self._congestion_bin_acc})
        self._trips_series.append({"sim_time": sim_time, "cumulative_arrivals": self._arrivals_count})
        self._congestion_bin_acc = 0.0

    def record_resource_sample(self, event: dict) -> None:
        layer = event.get("layer", "unknown")
        sample = {
            "timestamp": event.get("timestamp"),
            "vm_id": event.get("vm_id"),
            "process": event.get("process"),
            "cpu_pct": event.get("cpu_pct"),
            "ram_mb": event.get("ram_mb"),
            "ram_pct": event.get("ram_pct"),
            "gpu_pct": event.get("gpu_pct"),
            "gpu_mem_mb": event.get("gpu_mem_mb"),
            "net_in_mb": event.get("net_in_mb"),
            "net_out_mb": event.get("net_out_mb"),
        }
        self._resource_samples.setdefault(layer, []).append(sample)

    def ingest_event(self, event: dict) -> None:
        t = event.get("type")
        for k, v in (event.get("learner_state") or {}).items():
            self._config.setdefault(k, v)
        if t == "decision_latency":
            self._decision_latencies.append(event["latency_s"])
            for k in DECISION_SPLIT_KEYS:
                if event.get(k) is not None:
                    self._decision_split[k].append(event[k])
        elif t == "edge_travel":
            self._edge_travel_times.append(event["travel_time_s"])
        elif t == "learner_metrics" and event.get("algorithm") == self._algo.name:
            self._algorithm_series.append({"step": event["step"], **event["values"]})

    def write_final(self, sim_steps: int, sim_duration_s: float, wall_duration_s: float, routing_stats: dict,
                    loops: dict | None = None, throttle: dict | None = None) -> None:
        sim_elapsed_min = sim_duration_s / 60.0

        result: dict = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "group_id": self.group_id,
            "config": self._config,
            "sim_steps": sim_steps,
            "sim_duration_s": sim_duration_s,
            "wall_duration_s": round(wall_duration_s, 2),
        }
        if loops:
            result["loops"] = loops
        if throttle:
            result["throttle"] = throttle

        # ATT summary
        att_values = [e["att"] for e in self._att_series]
        if att_values:
            sorted_att = sorted(att_values)
            p95_idx = min(int(len(sorted_att) * 0.95), len(sorted_att) - 1)
            result["att"] = {
                "mean": round(statistics.mean(sorted_att), 3),
                "median": round(statistics.median(sorted_att), 3),
                "p95": round(sorted_att[p95_idx], 3),
                "count": len(sorted_att),
            }
        else:
            result["att"] = {"mean": None, "median": None, "p95": None, "count": 0}

        result["departures"] = self._departure_count
        arrived = result["att"]["count"]

        # ATT averages survivors only
        missing = max(0, self._departure_count - arrived)
        if arrived and result["att"]["mean"] is not None:
            censor = max(sim_duration_s, result["att"]["mean"] * 2.0)
            result["att"]["censored_mean"] = round(
                (result["att"]["mean"] * arrived + censor * missing) / (arrived + missing), 3
            )
        else:
            result["att"]["censored_mean"] = round(sim_duration_s, 3) if missing else None
        result["att"]["not_arrived"] = missing
        result["throughput_vehicles_per_sim_min"] = (
            round(arrived / sim_elapsed_min, 3) if sim_elapsed_min > 0 else 0.0
        )

        # Congestion total (sum of bins and partial final bin)
        total_congestion = sum(e["vehicle_seconds"] for e in self._congestion_series) + self._congestion_bin_acc
        result["congestion_vehicle_seconds"] = round(total_congestion, 2)

        # Routing stats from LatencyTracker
        result["routing"] = routing_stats

        # Round-trip latency
        if self._round_trip_latencies:
            sorted_rt = sorted(self._round_trip_latencies)
            p95_idx = min(int(len(sorted_rt) * 0.95), len(sorted_rt) - 1)
            result["round_trip_latency"] = {
                "mean": round(statistics.mean(sorted_rt), 4),
                "p95": round(sorted_rt[p95_idx], 4),
            }
        else:
            result["round_trip_latency"] = {"mean": None, "p95": None}

        # Decision latency (from Layer 3 events)
        if self._decision_latencies:
            sorted_dl = sorted(self._decision_latencies)
            p95_idx = min(int(len(sorted_dl) * 0.95), len(sorted_dl) - 1)
            result["decision_latency"] = {
                "mean": round(statistics.mean(sorted_dl), 4),
                "p95": round(sorted_dl[p95_idx], 4),
            }
        else:
            result["decision_latency"] = {"mean": None, "p95": None}
        result["decision_latency_split"] = {k: _mean_p95(v, 6) for k, v in self._decision_split.items()}

        # Edge travel time (from Layer 3 events)
        if self._edge_travel_times:
            sorted_et = sorted(self._edge_travel_times)
            p95_idx = min(int(len(sorted_et) * 0.95), len(sorted_et) - 1)
            result["edge_travel_time"] = {
                "mean": round(statistics.mean(sorted_et), 3),
                "p95": round(sorted_et[p95_idx], 3),
                "count": len(sorted_et),
            }
        else:
            result["edge_travel_time"] = {"mean": None, "p95": None, "count": 0}

        # Trip distribution
        result["trip_distribution"] = _make_trip_distribution(self._trip_data)

        # Write series to file (at the bottom)
        result["trips_series"] = self._trips_series
        result["congestion_series"] = self._congestion_series
        result["att_series"] = self._att_series
        result["algorithm_metrics"] = {
            "series": self._algorithm_series,
            "summary": self._algo.summarize(self._algorithm_series) if self._algo.summarize else None,
        }
        result["resources"] = self._resource_samples


        out_file = self.output_path / f"{self.run_id}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

        print(f"[Metrics] Written to {out_file}")
