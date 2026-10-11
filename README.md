# Distributed Routing Platform

Algorithm-agnostic distributed platform for learning-based vehicle routing: a SUMO simulation,
a Kafka bus, Faust stream actors and a pluggable learner, coupled only through Kafka topics.
Routing algorithms are plugins in `algorithms/` (`dijkstra`, `dynamic_dijkstra`, `deep_q`).

> Installation documentation and a simplified (dockerized) setup are coming. For now this
> assumes Python 3.13, Docker and SUMO (`pip install eclipse-sumo==1.26.0`).

```bash
pip install -r requirements.txt
python startup_scripts/start_control_agents_local.py
python experiment_dashboard/app.py    # dashboard on http://localhost:5000
```

Kafka runs without TLS/SASL; in a multi-machine setup restrict the broker port by firewall.

The `enschede_centre_*` network is derived from OpenStreetMap data, © OpenStreetMap
contributors, ODbL. MIT licensed, see `LICENSE`.
