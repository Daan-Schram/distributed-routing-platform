from algorithms.base import Algorithm

ALGORITHM = Algorithm(
    name="dijkstra",
    label="Dijkstra (static)",
    router="algorithms.dijkstra.router:DijkstraRouter",
    reference=True,
)
