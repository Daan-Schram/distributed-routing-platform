from algorithms.base import Algorithm, Param

ALGORITHM = Algorithm(
    name="dynamic_dijkstra",
    label="Dynamic Dijkstra",
    router="algorithms.dynamic_dijkstra.router:DynamicDijkstraRouter",
    params=(
        Param("min_ratio", "Min speed ratio", "float", 0.1, bounds=(0.01, 1.0),
              help="Speed ratio floor, caps an edge at 1/min_ratio x its free-flow time"),
    ),
    needs_snapshot=True,
    reference=True,
)
