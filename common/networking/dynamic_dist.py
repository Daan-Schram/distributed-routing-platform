import math

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

_FALLBACK_SPEED = 14.0  # only for zero-length/zero-time edges


class DynamicDist:
    """Time to the destination per edge under observed speeds, the 6th Deep Q feature

    the weight of a->b is b's time, so an edge's own cost is not in its distance. Router, learner and harness all call this class
    """

    def __init__(self, ordered_edges: list[str], edge_map, connection_map, global_max_dist: float,
                 min_ratio: float = 0.1):
        idx = {e: i for i, e in enumerate(ordered_edges)}
        self._idx = idx
        self._n = len(ordered_edges)
        self._min_ratio = min_ratio
        self._time = np.array([edge_map[e][3] for e in ordered_edges], dtype=np.float64)
        self._freeflow = np.array(
            [(edge_map[e][2] / edge_map[e][3]) if edge_map[e][3] > 0 else _FALLBACK_SPEED for e in ordered_edges],
            dtype=np.float64,
        )
        self._unreachable = global_max_dist / min_ratio
        self._log_denom = math.log1p(global_max_dist)

        # Reverse line graph (b -> a for every connection a -> b), deduplicated per lane
        pairs = sorted({(idx[b], idx[a]) for a, succ in connection_map.items() if a in idx
                        for b in succ if b in idx})
        rows = np.array([p[0] for p in pairs], dtype=np.int64)
        self._src = rows
        self._indices = np.array([p[1] for p in pairs], dtype=np.int32)
        self._indptr = np.searchsorted(rows, np.arange(self._n + 1)).astype(np.int32)

    def edge_times(self, speeds) -> np.ndarray:
        """Edge travel time at the observed speeds, capped at 1/min_ratio x free flow"""
        s = np.asarray(speeds, dtype=np.float32).astype(np.float64)
        ratio = np.clip(s / self._freeflow, self._min_ratio, 1.0)
        return self._time / ratio

    def seconds(self, speeds, dest_edge: str) -> np.ndarray:
        d_idx = self._idx.get(dest_edge)
        if d_idx is None:
            return np.full(self._n, self._unreachable)
        w = self.edge_times(speeds)
        graph = csr_matrix((w[self._src], self._indices, self._indptr), shape=(self._n, self._n))
        d = dijkstra(graph, directed=True, indices=d_idx)
        d[~np.isfinite(d)] = self._unreachable
        return d

    def feature(self, speeds, dest_edge: str) -> np.ndarray:
        return (np.log1p(self.seconds(speeds, dest_edge)) / self._log_denom).astype(np.float32)
