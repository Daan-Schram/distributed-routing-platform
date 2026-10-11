import importlib
from dataclasses import dataclass, field
from typing import Any, Callable


def import_string(path: str):
    module, _, attr = path.partition(":")
    mod = importlib.import_module(module)
    return getattr(mod, attr) if attr else mod


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    type: str
    default: Any
    bounds: tuple | None = None
    scale: str = "linear"
    search: tuple | None = None
    search_default: bool = False
    choices: tuple = ()
    group: str = ""
    help: str = ""
    visible_if: dict | None = None

    def __post_init__(self):
        self.coerce(self.default)
        if self.search is not None and self.type in ("int", "float"):
            for v in self.search:
                self.coerce(v)

    def coerce(self, value):
        if self.type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{self.name}: expected a bool, got {value!r}")
            return value
        if self.type == "choice":
            if value not in self.choices:
                raise ValueError(f"{self.name}: {value!r} is not one of {list(self.choices)}")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.name}: expected a number, got {value!r}")
        if self.type == "int":
            if value != int(value):
                raise ValueError(f"{self.name}: expected an integer, got {value!r}")
            value = int(value)
        else:
            value = float(value)
        if self.bounds and not self.bounds[0] <= value <= self.bounds[1]:
            raise ValueError(f"{self.name}: {value} is outside {list(self.bounds)}")
        return value

    def descriptor(self) -> dict:
        return {
            "name": self.name, "label": self.label, "type": self.type, "default": self.default,
            "bounds": list(self.bounds) if self.bounds else None, "scale": self.scale,
            "search": list(self.search) if self.search is not None else None,
            "search_default": self.search_default, "choices": list(self.choices),
            "group": self.group, "help": self.help, "visible_if": self.visible_if,
        }


@dataclass(frozen=True)
class Algorithm:
    name: str
    label: str
    router: str
    learner: str | None = None
    params: tuple[Param, ...] = ()
    checkpoint: str | None = None
    checkpoint_info: str | None = None
    needs_snapshot: bool = False
    bottleneck: bool = True
    reference: bool = False
    series: tuple[dict, ...] = ()
    summary: tuple[dict, ...] = ()
    summarize: Callable[[list], dict | None] | None = field(default=None, compare=False)
    frontend: str | None = None

    @property
    def learns(self) -> bool:
        return self.learner is not None

    def resolve(self, given: dict | None = None) -> dict:
        given = dict(given or {})
        by_name = {p.name: p for p in self.params}
        unknown = sorted(set(given) - set(by_name))
        if unknown:
            raise ValueError(f"{self.name} has no hyperparameter {', '.join(unknown)}")
        return {p.name: p.coerce(given[p.name]) if p.name in given else p.default for p in self.params}

    def make_router(self, line_graph, edge_map, connection_map, hp: dict):
        return import_string(self.router)(line_graph, edge_map, connection_map, hp)

    def descriptor(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "capabilities": {"learns": self.learns, "checkpoint": self.checkpoint is not None,
                             "needs_snapshot": self.needs_snapshot, "bottleneck": self.bottleneck,
                             "reference": self.reference},
            "params": [p.descriptor() for p in self.params],
            "metrics": {"series": list(self.series), "summary": list(self.summary)},
            "frontend": self.frontend,
        }


class Router:
    def find_next_edge(self, current_edge: str, dest_edge: str, vehicle_id: str = "") -> str | None:
        raise NotImplementedError

    def update_traffic_state(self, snapshot: dict[str, dict]) -> None:
        pass

    def apply_update(self, msg: dict) -> None:
        pass

    def clear_vehicle(self, vehicle_id: str) -> None:
        pass
