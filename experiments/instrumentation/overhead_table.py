import argparse
import glob
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from common.config import EXPERIMENTS_DIR, INSTRUMENTATION_DIR

RUN_METRICS = [
    ("wall time (s)", lambda d: d["wall_duration_s"]),
    ("decisions/s", lambda d: d["routing"]["total"] / d["wall_duration_s"]),
    ("RTT mean (ms)", lambda d: 1000 * d["round_trip_latency"]["mean"]),
    ("RTT p95 (ms)", lambda d: 1000 * d["round_trip_latency"]["p95"]),
    ("decision latency mean (ms)", lambda d: 1000 * d["decision_latency"]["mean"]),
    ("decision latency p95 (ms)", lambda d: 1000 * d["decision_latency"]["p95"]),
    ("on-time (%)", lambda d: d["routing"]["on_time_pct"]),
    ("truly unaccounted", lambda d: d["routing"]["truly_unaccounted"]),
    ("ATT mean (s)", lambda d: d["att"]["mean"]),
]


def _load(path):
    with open(path) as f:
        return json.load(f)


def _sim_window(sc):
    start = sc["started_at"] + (sc["startup"]["total_s"] or 0)
    dur = (sc.get("run") or {}).get("startup_done_to_sim_stopped_s")
    return (start, start + dur) if dur else None


def _topic_rates(topics, win):
    out = {}
    for tf in topics:
        bin_s = tf.get("bin_s", 60)
        bins = tf.get("per_bin") or {t: [{"bin": r["minute"], **r} for r in rows]
                                     for t, rows in tf.get("per_minute", {}).items()}
        first = int(-(-(win[0] - tf["started_at"]) // bin_s))
        last = int((win[1] - tf["started_at"]) // bin_s)
        if last <= first:
            continue
        for topic, rows in bins.items():
            o = out.setdefault(topic, [0, 0, 0.0])
            for r in rows:
                if first <= r["bin"] < last:
                    o[0] += r["messages"]
                    o[1] += r["bytes"]
        for o in out.values():
            o[2] += (last - first) * bin_s
    return {t: (60 * m / s, 60 * b / s / 1024) for t, (m, b, s) in out.items() if s}


def _proc_means(procs, win):
    out = {}
    for label, pf in procs.items():
        rows = [s for s in pf["samples"] if win[0] <= pf["started_at"] + s["t"] <= win[1]]
        for role in sorted({r for s in rows for r in s["roles"]}):
            vals = [s["roles"][role] for s in rows if role in s["roles"]]
            out[f"{label}/{role}"] = (statistics.mean(v["rss_mb"] for v in vals),
                                      statistics.mean(v["cpu_pct"] for v in vals))
    return out


def _fmt(vals):
    if not vals:
        return "–"
    m = statistics.mean(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return f"{m:.1f} ± {sd:.1f}" if abs(m) < 1000 else f"{m:.0f} ± {sd:.0f}"


def _delta(on, off):
    if not on or not off or statistics.mean(off) == 0:
        return "–"
    return f"{100 * (statistics.mean(on) - statistics.mean(off)) / statistics.mean(off):+.1f} %"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default=EXPERIMENTS_DIR, help="folder with result JSONs")
    ap.add_argument("--sidecars", default=INSTRUMENTATION_DIR, help="folder with <queue_item_id>_r<n>.json")
    ap.add_argument("--runs", nargs="*", help="run ids to use (default: every JSON in --results)")
    ap.add_argument("--topics", nargs="*", default=[], help="topic_counter JSONs")
    ap.add_argument("--procs", nargs="*", default=[], help="proc_sampler JSONs as label=path")
    ap.add_argument("--latex", action="store_true", help="also print the main table as LaTeX rows")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    topics = [_load(p) for p in args.topics]
    procs = {}
    for spec in args.procs:
        label, _, path = spec.rpartition("=")
        procs[label or os.path.splitext(os.path.basename(path))[0]] = _load(path)

    runs = []
    for path in sorted(glob.glob(os.path.join(args.results, "*.json"))):
        d = _load(path)
        if "config" not in d or (args.runs and d.get("run_id") not in args.runs):
            continue
        c = d["config"]
        sc_path = os.path.join(args.sidecars, f"{c.get('queue_item_id')}_r{c.get('repeat_index')}.json")
        sc = _load(sc_path) if os.path.exists(sc_path) else None
        runs.append((d, sc))

    cells: dict[tuple, list] = {}
    for d, sc in runs:
        c = d["config"]
        cells.setdefault((c["algorithm"], bool(c.get("telemetry", True))), []).append((d, sc))

    for alg in sorted({a for a, _ in cells}):
        on, off = cells.get((alg, True), []), cells.get((alg, False), [])
        print(f"\n## {alg}: telemetry on n={len(on)}, off n={len(off)}\n")
        for label, runs_ in (("on", on), ("off", off)):
            print(f"- {label}: " + ", ".join(f"{d['run_id']} (seed {d['config'].get('sumo_seed')})" for d, _ in runs_))
        print("\n| metric | on | off | Δ |\n|---|---|---|---|")
        rows = []
        for name, fn in RUN_METRICS:
            v_on, v_off = [fn(d) for d, _ in on], [fn(d) for d, _ in off]
            rows.append((name, _fmt(v_on), _fmt(v_off), _delta(v_on, v_off)))
            print(f"| {name} | {rows[-1][1]} | {rows[-1][2]} | {rows[-1][3]} |")

        print("\n| telemetry cost | on | off |\n|---|---|---|")
        side = {}
        for label, runs_ in (("on", on), ("off", off)):
            for d, sc in runs_:
                acc = side.setdefault(label, {})
                acc.setdefault("resource samples in JSON", []).append(
                    sum(len(v) if isinstance(v, list) else v for v in (d.get("resources") or {}).values()))
                if not sc:
                    continue
                acc.setdefault("startup total (s)", []).append(sc["startup"]["total_s"])
                for st in sc["startup"]["steps"]:
                    acc.setdefault(f"startup {st['step']} (s)", []).append(st["duration_s"])
                win = _sim_window(sc)
                if not win:
                    continue
                span = win[1] - win[0]
                acc.setdefault("SSE in (KiB/s)", []).append(
                    sum(v["bytes"] for v in sc["sse_in"].values()) / 1024 / span)
                acc.setdefault("SSE out (events/s)", []).append(sc["sse_out"]["events"] / span)
                acc.setdefault("SSE out (KiB/s)", []).append(sc["sse_out"]["bytes"] / 1024 / span)
                for topic, (mpm, kpm) in _topic_rates(topics, win).items():
                    acc.setdefault(f"{topic} (msgs/min)", []).append(mpm)
                    acc.setdefault(f"{topic} (KiB/min)", []).append(kpm)
                for role, (rss, cpu) in _proc_means(procs, win).items():
                    acc.setdefault(f"{role} RSS (MB)", []).append(rss)
                    acc.setdefault(f"{role} CPU (%)", []).append(cpu)
        for key in sorted({k for v in side.values() for k in v}):
            print(f"| {key} | {_fmt(side.get('on', {}).get(key))} | {_fmt(side.get('off', {}).get(key))} |")

        if args.latex:
            print("\n% LaTeX: metric & on & off & delta")
            for name, a, b, dl in rows:
                print(f"{name} & {a.replace('±', '$\\pm$')} & {b.replace('±', '$\\pm$')} & {dl.replace('%', '\\%')} \\\\")


if __name__ == "__main__":
    main()
