import json
import os
from pathlib import Path

from algorithms.base import Algorithm, Param, Router, import_string
from algorithms import dijkstra, dynamic_dijkstra, deep_q

ALGORITHMS: dict[str, Algorithm] = {a.name: a for a in (
    dijkstra.ALGORITHM,
    dynamic_dijkstra.ALGORITHM,
    deep_q.ALGORITHM,
)}

PLATFORM_TUNABLES: tuple[Param, ...] = (
    Param("speed_ema_alpha", "Speed EMA alpha", "float", 0.2, bounds=(0.01, 1.0), scale="log",
          search=(0.03, 0.3), search_default=True,
          help="EMA on broadcast edge speeds; match it between runs that read the snapshot"),
)


def get(name: str) -> Algorithm:
    if name not in ALGORITHMS:
        raise ValueError(f"unknown algorithm: {name}")
    return ALGORITHMS[name]


def names() -> list[str]:
    return list(ALGORITHMS)


def descriptors() -> dict:
    return {
        "platform_tunables": [p.descriptor() for p in PLATFORM_TUNABLES],
        "algorithms": [a.descriptor() for a in ALGORITHMS.values()],
    }


def checkpoint_path(name: str) -> str | None:
    from common.config import CHECKPOINTS_DIR
    algo = get(name)
    return str(Path(CHECKPOINTS_DIR) / algo.checkpoint) if algo.checkpoint else None


def resolve_env() -> tuple[Algorithm, dict]:
    algo = get(os.getenv("ALGORITHM", "dijkstra"))
    return algo, algo.resolve(json.loads(os.getenv("HYPERPARAMS") or "{}"))


def _check() -> None:
    platform = {p.name for p in PLATFORM_TUNABLES}
    for algo in ALGORITHMS.values():
        names_ = [p.name for p in algo.params]
        if len(names_) != len(set(names_)) or platform & set(names_):
            raise ValueError(f"{algo.name}: duplicate hyperparameter name")


_check()
