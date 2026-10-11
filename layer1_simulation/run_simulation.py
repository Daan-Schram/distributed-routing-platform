import json
import os
import sys
import uuid
import time
from pathlib import Path

import traci
import traci.constants as tc
from kafka import KafkaProducer, KafkaConsumer

import algorithms
from common.config import (
    NETWORK_NAME, NETWORK_FILE, ROUTE_FILE, KAFKA_BOOTSTRAP,
    TOPIC_ROUTE_RESPONSES, TOPIC_METRICS, TOPIC_RESOURCE_METRICS, EXPERIMENTS_DIR, RUN_ID_FILE,
    USE_GUI, BROADCAST_RATE, STATS_PRINT_INTERVAL, SNAPSHOT_INTERVAL,
    NUM_WORKERS, CONGESTION_SPEED_THRESHOLD, CAPTURE_TRAJECTORIES, TRAJECTORIES_DIR,
    JAM_TOP_EDGES, JAM_FACTOR, SUMO_SEED, TELEMETRY, SPEED_EMA_ALPHA,
)
from common.monitoring.resource_monitor import ResourceMonitor
from layer1_simulation.metrics import MetricsStore, make_run_id
from layer1_simulation.simulation_bridge import SimulationBridge
from layer1_simulation.vehicle_tracker import VehicleTracker
from layer1_simulation.trajectory_recorder import TrajectoryRecorder
from layer1_simulation.bottleneck import edge_usage, apply_jam, JamTracker


def get_sumo_tools_path() -> str:
    try:
        import sumo as _sumo_pkg
        return os.path.join(_sumo_pkg.SUMO_HOME, "tools")
    except ImportError:
        pass
    if "SUMO_HOME" in os.environ:
        return os.path.join(os.environ["SUMO_HOME"], "tools")
    sys.exit("Please declare environment variable 'SUMO_HOME'")


def get_sumo_binary(gui: bool) -> str:
    name = "sumo-gui" if gui else "sumo"
    try:
        import sumo as _sumo_pkg
        candidate = os.path.join(_sumo_pkg.SUMO_HOME, "bin", name)
        for path in (candidate, candidate + ".exe"):
            if os.path.isfile(path):
                return path
    except ImportError:
        pass
    return name


def setup_kafka_producer():
    """Create and return a JSON-serializing Kafka producer"""
    try:
        producer = KafkaProducer(
            bootstrap_servers=[KAFKA_BOOTSTRAP],
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )
        print(f"Connected for producing to Kafka on {KAFKA_BOOTSTRAP}")
        return producer
    except Exception as e:
        print(f"KAFKA PRODUCER ERROR: {e}")
        sys.exit(1)


def setup_kafka_consumer():
    """Create and return a JSON-deserializing Kafka consumer for route_responses"""
    unique_group_id = f"sumo_client_{uuid.uuid4()}"
    try:
        consumer = KafkaConsumer(
            TOPIC_ROUTE_RESPONSES,
            bootstrap_servers=[KAFKA_BOOTSTRAP],
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            auto_offset_reset="latest",
            group_id=unique_group_id,
        )
        print(f"Connected for consuming from Kafka, group: {unique_group_id}")
        return consumer
    except Exception as e:
        print(f"KAFKA CONSUMER ERROR: {e}")
        sys.exit(1)


def setup_metrics_consumer():
    unique_group_id = f"sumo_metrics_{uuid.uuid4()}"
    try:
        consumer = KafkaConsumer(
            TOPIC_METRICS,
            bootstrap_servers=[KAFKA_BOOTSTRAP],
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            auto_offset_reset="latest",
            group_id=unique_group_id,
        )
        return consumer
    except Exception as e:
        print(f"KAFKA METRICS CONSUMER ERROR: {e}")
        sys.exit(1)


def setup_resource_metrics_consumer():
    unique_group_id = f"sumo_resource_{uuid.uuid4()}"
    try:
        consumer = KafkaConsumer(
            TOPIC_RESOURCE_METRICS,
            bootstrap_servers=[KAFKA_BOOTSTRAP],
            value_deserializer=lambda m: json.loads(m.decode("utf-8")),
            auto_offset_reset="latest",
            group_id=unique_group_id,
        )
        return consumer
    except Exception as e:
        print(f"KAFKA RESOURCE METRICS CONSUMER ERROR: {e}")
        sys.exit(1)


def _count_route_vehicles(route_file: str) -> int | None:
    import xml.etree.ElementTree as ET
    try:
        return sum(1 for _ in ET.parse(route_file).getroot().iter("vehicle"))
    except Exception:
        return None


def run():
    sys.path.append(get_sumo_tools_path())
    monitor = ResourceMonitor(layer="layer1", process="run_simulation")
    monitor.start()
    producer = setup_kafka_producer()
    consumer = setup_kafka_consumer()
    metrics_consumer = setup_metrics_consumer()
    resource_metrics_consumer = setup_resource_metrics_consumer() if TELEMETRY else None

    sumo_binary = get_sumo_binary(USE_GUI)
    sumo_cmd = [
        sumo_binary,
        "-n", NETWORK_FILE,
        "-r", ROUTE_FILE,
        "--no-step-log", "true",
        "--waiting-time-memory", "1000",
        "--max-depart-delay", "30",
    ]
    if SUMO_SEED is not None:
        sumo_cmd += ["--seed", str(SUMO_SEED)]

    import subprocess as _subprocess
    _orig_Popen = _subprocess.Popen
    class _SumoPopens(_subprocess.Popen):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('stderr', _subprocess.DEVNULL)
            super().__init__(*args, **kwargs)
    _subprocess.Popen = _SumoPopens
    try:
        traci.start(sumo_cmd)
    finally:
        _subprocess.Popen = _orig_Popen
    print("Simulation started...")

    sim_start_time = time.time()

    jam_info = None
    jam_tracker = None
    jam_usage: dict = {}
    if JAM_TOP_EDGES > 0:
        jam_usage = dict(edge_usage(ROUTE_FILE))
        _total = sum(jam_usage.values())
        _edges = [e for e, _ in sorted(jam_usage.items(), key=lambda kv: -kv[1])[:JAM_TOP_EDGES]]
        _share = round(sum(jam_usage[e] for e in _edges) / _total, 4) if _total else 0.0
        _applied = apply_jam(_edges, JAM_FACTOR)
        if _applied:
            jam_info = {"edges": _applied, "factor": JAM_FACTOR,
                        "share_of_shortest_paths": _share,
                        "usage": jam_usage}
            jam_tracker = JamTracker(_applied)
            print(f"[Bottleneck] Slowed {len(_applied)} edges by {JAM_FACTOR}x "
                  f"({_share:.1%} of shortest paths): {', '.join(_applied)}")
        else:
            print(f"[Bottleneck] JAM_TOP_EDGES={JAM_TOP_EDGES} requested but no edge was slowed")

    algo, hp = algorithms.resolve_env()
    group_id = os.getenv("GROUP_ID", "")
    run_id = make_run_id(algo.name)
    config = {
        "algorithm": algo.name,
        "hyperparams": hp,
        "num_workers": NUM_WORKERS,
        "num_actor_vms": int(os.getenv("NUM_ACTOR_VMS", "1")),
        "network_name": NETWORK_NAME,
        "network_file": os.path.basename(NETWORK_FILE),
        "route_file": os.path.basename(ROUTE_FILE),
        "congestion_speed_threshold": CONGESTION_SPEED_THRESHOLD,
        "broadcast_rate": BROADCAST_RATE,
        "speed_ema_alpha": SPEED_EMA_ALPHA,
        "snapshot_interval_s": SNAPSHOT_INTERVAL,
        "use_gui": USE_GUI,
        "capture_trajectories": CAPTURE_TRAJECTORIES,
        "sumo_seed": SUMO_SEED,
        "telemetry": TELEMETRY,
        "log_proxies": (os.getenv("LOG_PROXIES", "").lower() == "true") if os.getenv("LOG_PROXIES") else TELEMETRY,
        "bottleneck": (f"{len(jam_info['edges'])} edges x{jam_info['factor']:g}" if jam_info
                       else ("requested but not applied" if JAM_TOP_EDGES > 0 else "none")),
        "jam_requested": JAM_TOP_EDGES,
        "jam_factor": JAM_FACTOR if JAM_TOP_EDGES > 0 else None,
        "vehicles_in_route_file": _count_route_vehicles(ROUTE_FILE),
        "bottleneck_edges": ", ".join(jam_info["edges"]) if jam_info else "",
        "bottleneck_path_share": jam_info["share_of_shortest_paths"] if jam_info else None,
    }
    if os.getenv("QUEUE_ITEM_ID"):
        _ckpt_before = os.getenv("CHECKPOINT_TRAIN_STEPS_BEFORE")
        config.update({
            "queue_item_id": os.getenv("QUEUE_ITEM_ID"),
            "repeat_index": int(os.getenv("REPEAT_INDEX", "1")),
            "repeats": int(os.getenv("REPEATS", "1")),
            "warm_start": os.getenv("WARM_START", "false").lower() == "true",
            "checkpoint_exists_before": (os.getenv("CHECKPOINT_EXISTS_BEFORE").lower() == "true"
                                         if os.getenv("CHECKPOINT_EXISTS_BEFORE") is not None else None),
            "checkpoint_train_steps_before": int(_ckpt_before) if _ckpt_before else None,
        })
    if jam_info:
        config["jam"] = dict(jam_info)
    metrics = MetricsStore(run_id, algo, EXPERIMENTS_DIR, config=config, group_id=group_id)
    run_id_file = Path(RUN_ID_FILE)
    run_id_file.parent.mkdir(parents=True, exist_ok=True)
    run_id_file.write_text(run_id)

    tracker = VehicleTracker(metrics)
    bridge = SimulationBridge(producer, consumer, metrics=metrics)
    trajectory_recorder = TrajectoryRecorder(enabled=CAPTURE_TRAJECTORIES)

    _max_runtime_s = int(os.getenv("MAX_RUNTIME_S", "0")) or None
    step = 0
    total_routed = 0
    timed_out = False

    while traci.simulation.getMinExpectedNumber() > 0:
        traci.simulationStep()
        bridge.poll_teleports()

        for veh_id in traci.simulation.getDepartedIDList():
            traci.vehicle.subscribe(veh_id, [
                tc.VAR_ROAD_ID,
                tc.VAR_LANE_ID,
                tc.VAR_LANEPOSITION,
                tc.VAR_SPEED,
                tc.VAR_DISTANCE,
            ])
            bridge.register_vehicle(veh_id)
            tracker.on_departed(veh_id, traci.simulation.getTime())
            if CAPTURE_TRAJECTORIES:
                route = traci.vehicle.getRoute(veh_id)
                if route:
                    trajectory_recorder.record_destination(veh_id, route[-1])

        vehicle_data = traci.vehicle.getAllSubscriptionResults()

        tracker.process_step(vehicle_data, traci.simulation.getTime())
        trajectory_recorder.record_step(vehicle_data)
        if jam_tracker:
            jam_tracker.observe(vehicle_data)

        for veh_id in traci.simulation.getArrivedIDList():
            bridge.check_arrival(veh_id)
            tracker.on_arrived(veh_id, traci.simulation.getTime())
            if jam_tracker:
                jam_tracker.on_arrived(veh_id)

        if step > 0 and (step % SNAPSHOT_INTERVAL) == 0:
            metrics.record_snapshot(traci.simulation.getTime())

        if (step % BROADCAST_RATE) == 0:
            bridge.broadcast_state(traci.edge.getIDList(), step)

        bridge.check_and_send_requests(step, vehicle_data)
        bridge.detect_and_send_edge_transitions(vehicle_data)
        total_routed += bridge.receive_responses(step)

        for _tp, _msgs in metrics_consumer.poll(timeout_ms=0).items():
            for _msg in _msgs:
                metrics.ingest_event(_msg.value)
                if _msg.value.get("type") == "training_pressure":
                    bridge.set_training_pressure(
                        _msg.value.get("poll_count", 0),
                        _msg.value.get("cap", 100),
                    )

        if resource_metrics_consumer:
            for _tp, _msgs in resource_metrics_consumer.poll(timeout_ms=0).items():
                for _msg in _msgs:
                    metrics.record_resource_sample(_msg.value)

        active_vehicles = set(traci.vehicle.getIDList())
        bridge.cleanup_pending(active_vehicles)
        bridge.update_throttle()
        bridge.wait()

        if (step % STATS_PRINT_INTERVAL) == 0:
            print(f"[Step {step}] vehicles: {len(active_vehicles)} | {bridge.summary_line()}")

        step += 1
        if _max_runtime_s and (time.time() - sim_start_time) >= _max_runtime_s:
            timed_out = True
            print(f"[Timeout] Max runtime of {_max_runtime_s}s reached at step {step}, stopping.")
            break

    sim_end_time = time.time()
    total_time = sim_end_time - sim_start_time
    sim_time_s = traci.simulation.getTime()

    traci.close()

    # Drain remaining metric events before writing final
    for _tp, _msgs in metrics_consumer.poll(timeout_ms=2000).items():
        for _msg in _msgs:
            metrics.ingest_event(_msg.value)

    if resource_metrics_consumer:
        for _tp, _msgs in resource_metrics_consumer.poll(timeout_ms=2000).items():
            for _msg in _msgs:
                metrics.record_resource_sample(_msg.value)

    if jam_tracker:
        config["jam"].update(jam_tracker.stats())
    if timed_out:
        config["timed_out"] = True
        config["timed_out_at_s"] = round(time.time() - sim_start_time, 1)
    _line_graph = None
    if CAPTURE_TRAJECTORIES:
        from common.networking.network import load_network
        _, _line_graph, _, _ = load_network(NETWORK_FILE)
    loop_stats = trajectory_recorder.loop_stats(_line_graph)
    trajectory_recorder.write(TRAJECTORIES_DIR, run_id, os.path.basename(NETWORK_FILE),
                              line_graph=_line_graph)
    metrics.write_final(step, sim_time_s, total_time, bridge.routing_stats(), loops=loop_stats,
                        throttle=bridge.throttle_stats())
    producer.close()
    consumer.close()
    metrics_consumer.close()
    if resource_metrics_consumer:
        resource_metrics_consumer.close()

    print("Simulation Finished.")
    print(f"Total Execution Time: {total_time:.2f} seconds")
    print(f"Avg Time per Step: {total_time/step:.4f} seconds")
    print(f"Final latency stats: {bridge.summary_line()}")


if __name__ == "__main__":
    run()
