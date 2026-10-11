"""
Local development launcher, starts all control agents as uvicorn subprocesses.

Usage:
  python startup_scripts/start_control_agents_local.py

Then start the dashboard:
  python experiment_dashboard/app.py

Port layout:
  8001 — layer1 (simulation)
  8002 — layer2 (Kafka)
  8003 — layer3 (Faust workers)
  8004 — layer4 (learner)
  5000 — experiment dashboard
"""
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AGENTS = [
    ("layer2_message_bus.control_agent:app", 8002),
    ("layer3_actors.control_agent:app", 8003),
    ("layer4_learner.control_agent:app", 8004),
    ("layer1_simulation.control_agent:app", 8001),
]


def main() -> None:
    procs = []
    for module, port in AGENTS:
        p = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", module,
             "--host", "0.0.0.0", "--port", str(port)],
            cwd=PROJECT_ROOT,
        )
        procs.append(p)
        print(f"  Started {module:<45} on port {port}  (pid {p.pid})")
        time.sleep(0.3)

    print()
    print("All control agents running. Start the dashboard in a separate terminal.")

    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        print("\nStopping all control agents...")
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
