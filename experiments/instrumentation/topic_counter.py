"""
Counts the messages and bytes per Kafka topic per time bin (default one minute), with its own consumer group
python experiments/instrumentation/topic_counter.py --seconds 900 --out output/instrumentation/topics_on_r1.json
"""
import argparse
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from kafka import KafkaConsumer, TopicPartition

from common.config import INSTRUMENTATION_DIR, KAFKA_BOOTSTRAP


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bootstrap", default=KAFKA_BOOTSTRAP)
    ap.add_argument("--seconds", type=float, default=0, help="stop after this many seconds (0: until Ctrl+C)")
    ap.add_argument("--bin", type=float, default=60, help="bin width in seconds")
    ap.add_argument("--since", type=float, default=None, help="count from this epoch time instead of now (retained topics)")
    ap.add_argument("--until", type=float, default=None, help="ignore messages after this epoch time")
    ap.add_argument("--out", default=None, help="output JSON (default: output/instrumentation/topics_<time>.json)")
    args = ap.parse_args()
    out = args.out or os.path.join(INSTRUMENTATION_DIR, f"topics_{time.strftime('%Y%m%d_%H%M%S')}.json")

    consumer = KafkaConsumer(
        bootstrap_servers=[args.bootstrap],
        group_id=f"topic_counter_{uuid.uuid4().hex[:8]}",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        metadata_max_age_ms=5000,
    )
    t0 = time.time()
    started = args.since if args.since is not None else t0
    start_ms = int(started * 1000)
    ends = None
    if args.since is None:
        consumer.subscribe(pattern=r"^[^_].*")
    else:
        tps = [TopicPartition(t, p) for t in consumer.topics() if not t.startswith("_")
               for p in consumer.partitions_for_topic(t) or []]
        consumer.assign(tps)
        ends = consumer.end_offsets(tps)
        for tp, o in consumer.offsets_for_times({tp: start_ms for tp in tps}).items():
            consumer.seek(tp, o.offset if o else ends[tp])
    until_ms = int(args.until * 1000) if args.until is not None else None
    last: dict[tuple, tuple[int, int]] = {}
    per_bin: dict[str, dict[int, list[int]]] = {}
    print(f"[topic_counter] counting on {args.bootstrap}, writing {out} at the end (Ctrl+C to stop)")
    try:
        while not args.seconds or time.time() - t0 < args.seconds:
            if ends and all(consumer.position(tp) >= e for tp, e in ends.items()):
                break
            for tp, msgs in consumer.poll(timeout_ms=1000).items():
                for m in msgs:
                    if m.timestamp < start_ms or (until_ms and m.timestamp > until_ms):
                        continue
                    prev = last.get((tp.topic, tp.partition))
                    if prev and m.offset <= prev[0] and m.timestamp <= prev[1]:
                        continue
                    last[(tp.topic, tp.partition)] = (m.offset, m.timestamp)
                    b = int((m.timestamp - start_ms) // (args.bin * 1000))
                    size = max(0, m.serialized_value_size) + max(0, m.serialized_key_size)
                    c = per_bin.setdefault(tp.topic, {}).setdefault(b, [0, 0])
                    c[0] += 1
                    c[1] += size
    except KeyboardInterrupt:
        pass
    finally:
        consumer.close()

    ended = time.time()
    result = {
        "bootstrap": args.bootstrap,
        "started_at": started,
        "ended_at": ended,
        "duration_s": round(ended - started, 1),
        "bin_s": args.bin,
        "totals": {t: {"messages": sum(v[0] for v in mins.values()), "bytes": sum(v[1] for v in mins.values())}
                   for t, mins in sorted(per_bin.items())},
        "per_bin": {t: [{"bin": k, "messages": v[0], "bytes": v[1]} for k, v in sorted(mins.items())]
                    for t, mins in sorted(per_bin.items())},
    }
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    for t, tot in result["totals"].items():
        print(f"  {t:<40} {tot['messages']:>9} msgs {tot['bytes'] / 1e6:>9.2f} MB")
    print(f"[topic_counter] wrote {out}")


if __name__ == "__main__":
    main()
