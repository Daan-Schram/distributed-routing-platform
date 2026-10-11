"""
Samples RSS and CPU of the platforms processes and Kafka container; reads only.
python experiments/instrumentation/proc_sampler.py --interval 5 --seconds 900 --out output/instrumentation/procs_on_r1.json
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psutil

from common.config import INSTRUMENTATION_DIR
from common.monitoring.resource_monitor import _parse_size_mb

ROLES = [
    ("dashboard", ["experiment_dashboard/app.py", "experiment_dashboard.app"]),
    ("agent_layer1", ["layer1_simulation.control_agent", "layer1_simulation/control_agent"]),
    ("agent_layer2", ["layer2_message_bus.control_agent", "layer2_message_bus/control_agent"]),
    ("agent_layer3", ["layer3_actors.control_agent", "layer3_actors/control_agent"]),
    ("agent_layer4", ["layer4_learner.control_agent", "layer4_learner/control_agent"]),
    ("simulation", ["run_simulation"]),
    ("faust_worker", ["layer3_actors.agent_worker"]),
    ("faust_launcher", ["start_faust.py"]),
    ("learner", ["start_learner.py"]),
]
SUMO_NAMES = {"sumo", "sumo.exe", "sumo-gui", "sumo-gui.exe"}


def _role(p: psutil.Process) -> str | None:
    try:
        if p.name().lower() in SUMO_NAMES:
            return "sumo"
        cmd = " ".join(p.cmdline()).replace("\\", "/")
    except (psutil.Error, OSError):
        return None
    for role, subs in ROLES:
        if any(s in cmd for s in subs):
            return role
    return None


def _docker_stats(container: str) -> dict | None:
    try:
        r = subprocess.run(["docker", "stats", container, "--no-stream", "--format", "{{json .}}"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        d = json.loads(r.stdout.strip())
        return {"n": 1, "rss_mb": round(_parse_size_mb(d["MemUsage"].split("/")[0]), 1),
                "cpu_pct": float(d["CPUPerc"].rstrip("%"))}
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--seconds", type=float, default=0, help="stop after this many seconds (0: until Ctrl+C)")
    ap.add_argument("--kafka-container", default="kafka", help="docker container to sample ('' to skip)")
    ap.add_argument("--out", default=None, help="output JSON (default: output/instrumentation/procs_<time>.json)")
    args = ap.parse_args()
    out = args.out or os.path.join(INSTRUMENTATION_DIR, f"procs_{time.strftime('%Y%m%d_%H%M%S')}.json")

    tracked: dict[int, tuple[psutil.Process, str]] = {}
    samples = []
    started = time.time()
    print(f"[proc_sampler] sampling every {args.interval}s, writing {out} at the end (Ctrl+C to stop)")
    try:
        while not args.seconds or time.time() - started < args.seconds:
            t = time.time()
            for p in psutil.process_iter():
                if p.pid not in tracked:
                    role = _role(p)
                    if role:
                        p.cpu_percent(None)
                        tracked[p.pid] = (p, role)
            roles: dict[str, dict] = {}
            for pid, (p, role) in list(tracked.items()):
                try:
                    with p.oneshot():
                        rss = p.memory_info().rss / 1024 / 1024
                        cpu = p.cpu_percent(None)
                except (psutil.Error, OSError):
                    del tracked[pid]
                    continue
                r = roles.setdefault(role, {"n": 0, "rss_mb": 0.0, "cpu_pct": 0.0})
                r["n"] += 1
                r["rss_mb"] = round(r["rss_mb"] + rss, 1)
                r["cpu_pct"] = round(r["cpu_pct"] + cpu, 1)
            if args.kafka_container:
                k = _docker_stats(args.kafka_container)
                if k:
                    roles["kafka_container"] = k
            samples.append({"t": round(t - started, 1), "roles": roles})
            time.sleep(max(0.0, args.interval - (time.time() - t)))
    except KeyboardInterrupt:
        pass

    summary = {}
    for role in sorted({r for s in samples for r in s["roles"]}):
        vals = [s["roles"][role] for s in samples if role in s["roles"]]
        summary[role] = {
            "samples": len(vals),
            "rss_mb_mean": round(sum(v["rss_mb"] for v in vals) / len(vals), 1),
            "rss_mb_max": max(v["rss_mb"] for v in vals),
            "cpu_pct_mean": round(sum(v["cpu_pct"] for v in vals) / len(vals), 1),
            "n_max": max(v["n"] for v in vals),
        }
    result = {"started_at": started, "duration_s": round(time.time() - started, 1), "interval_s": args.interval,
              "cpu_count": psutil.cpu_count(), "summary": summary, "samples": samples}
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    for role, s in summary.items():
        print(f"  {role:<16} n={s['n_max']:<3} rss mean {s['rss_mb_mean']:>8.1f} MB  max {s['rss_mb_max']:>8.1f} MB"
              f"  cpu mean {s['cpu_pct_mean']:>6.1f} %")
    print(f"[proc_sampler] wrote {out}")


if __name__ == "__main__":
    main()
