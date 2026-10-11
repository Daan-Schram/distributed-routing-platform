import time
from collections import deque

INITIAL_POLL_TIMEOUT_MS = 100 # Starting Kafka poll timeout per step (ms)
MAX_POLL_TIMEOUT_MS = 2000 # Hard cap on poll timeout (ms)
TARGET_TIMEOUT_RATE = 0.05 # Target timed-out rate (Kafka too slow)

INITIAL_STEP_DELAY_MS = 0 # Starting step delay (ms)
MAX_STEP_DELAY_MS = 200
TARGET_TOO_LATE_RATE = 0.01 # Target too-late rate (simulation too fast)

MAX_TRAINING_DELAY_S = 0.5 # Maximum extra step delay for training to catch up

THROTTLE_WINDOW = 50 # Rolling window size (steps) for rate calculation


class LatencyTracker:
    """
    Tracks route-response latency across simulation steps and computes
    adaptive throttle parameters (Kafka poll timeout + step delay).

    The tracker maintains a rolling window of (on_time, timed_out, too_late)
    counts per step. Two signals are tracked independently:
    - timed_out: Kafka response arrived after MAX_LATENCY_THRESHOLD → increase poll_ms
    - too_late:  response was on time but vehicle already moved → increase step_delay_s
    When each rate falls below half the target, the corresponding parameter is
    relaxed back down.
    """

    def __init__(self, window_size=THROTTLE_WINDOW,
                 target_timeout_rate=TARGET_TIMEOUT_RATE,
                 target_too_late_rate=TARGET_TOO_LATE_RATE):
        self._window = deque(maxlen=window_size)  # (on_time, timed_out, too_late)
        self._target_timeout_rate  = target_timeout_rate
        self._target_too_late_rate = target_too_late_rate
        self._poll_ms = INITIAL_POLL_TIMEOUT_MS
        self._step_delay_s = INITIAL_STEP_DELAY_MS / 1000
        self._training_delay_s = 0.0

        self._wait_steps = 0
        self._step_delay_sum = 0.0
        self._step_delay_max = 0.0
        self._training_delay_sum = 0.0
        self._training_delay_max = 0.0

        self.total_on_time = 0
        self.total_timed_out = 0
        self.total_too_late = 0
        self.total_exceptions = 0

    def record(self, on_time: int, timed_out: int, too_late: int = 0, exceptions: int = 0) -> None:
        """Called every step within a window, with on-time, timed-out, too-late, and exception response counts"""
        self._window.append((on_time, timed_out, too_late))
        self.total_on_time   += on_time
        self.total_timed_out += timed_out
        self.total_too_late  += too_late
        self.total_exceptions += exceptions

    def update_throttle(self) -> tuple[int, float]:
        """
        Re-compute poll timeout and step delay independently.
        Returns (poll_timeout_ms, step_delay_s)

        poll_ms    reacts to timeout_rate  — Kafka is too slow, wait longer
        step_delay reacts to too_late_rate — simulation is too fast, slow it down
        """
        t_rate = self.timeout_rate
        tl_rate = self.too_late_rate

        # Adjust poll timeout based on timed-out rate
        if t_rate > self._target_timeout_rate + 0.10:
            self._poll_ms = min(self._poll_ms + 100, MAX_POLL_TIMEOUT_MS)
        elif t_rate > self._target_timeout_rate:
            self._poll_ms = min(self._poll_ms + 25, MAX_POLL_TIMEOUT_MS)
        elif t_rate < self._target_timeout_rate * 0.5:
            self._poll_ms = max(self._poll_ms - 25, INITIAL_POLL_TIMEOUT_MS)

        # Adjust step delay based on too-late rate
        if tl_rate > self._target_too_late_rate + 0.10:
            self._step_delay_s = min(self._step_delay_s + 0.010, MAX_STEP_DELAY_MS / 1000)
        elif tl_rate > self._target_too_late_rate:
            self._step_delay_s = min(self._step_delay_s + 0.002, MAX_STEP_DELAY_MS / 1000)
        elif tl_rate < self._target_too_late_rate * 0.5:
            self._step_delay_s = max(self._step_delay_s - 0.001, INITIAL_STEP_DELAY_MS / 1000)

        return self._poll_ms, self._step_delay_s

    def set_training_pressure(self, poll_count: int, cap: int) -> None:
        """Adjust extra step delay based on learner backlog.

        When poll_count > cap the learner consumed more experiences than it could train on — it is falling behind.  We slow the simulation proportionally
        """
        if poll_count <= cap:
            self._training_delay_s = 0.0
        else:
            # lag_ratio: 1.0 = just at capacity, 5.0 = five times behind (capped)
            lag_ratio = min(poll_count / max(cap, 1), 5.0)
            self._training_delay_s = (lag_ratio - 1.0) / 4.0 * MAX_TRAINING_DELAY_S

    def wait(self) -> None:
        """Sleep for the sum of the adaptive routing delay and training backpressure delay."""
        self._wait_steps += 1
        self._step_delay_sum += self._step_delay_s
        self._step_delay_max = max(self._step_delay_max, self._step_delay_s)
        self._training_delay_sum += self._training_delay_s
        self._training_delay_max = max(self._training_delay_max, self._training_delay_s)
        total = self._step_delay_s + self._training_delay_s
        if total > 0:
            time.sleep(total)

    def throttle_stats(self) -> dict:
        n = self._wait_steps

        def _block(total: float, peak: float) -> dict:
            return {"mean": round(total / n, 5) if n else None, "max": round(peak, 4), "total": round(total, 2)}

        return {
            "steps": n,
            "step_delay_s": _block(self._step_delay_sum, self._step_delay_max),
            "training_delay_s": _block(self._training_delay_sum, self._training_delay_max),
        }

    def summary_line(
        self,
        requests_sent: int | None = None,
        dropped_vehicle_gone: int | None = None,
        duplicate_requests: int | None = None,
        route_no_connection: int | None = None,
        route_junction_internal: int | None = None,
        route_other_error: int | None = None,
        already_at_edge: int | None = None,
    ) -> str:
        total = self.total_on_time + self.total_timed_out + self.total_too_late
        if total == 0:
            return "No route responses yet."
        unaccounted = (
            requests_sent - total - (dropped_vehicle_gone or 0) - (already_at_edge or 0)
        ) if requests_sent is not None else None
        route_fail_parts = []
        if route_no_connection:
            route_fail_parts.append(f"no_conn:{route_no_connection}")
        if route_junction_internal:
            route_fail_parts.append(f"jct_int:{route_junction_internal}")
        if route_other_error:
            route_fail_parts.append(f"other:{route_other_error}")
        route_fail_str = f" | route_fail({', '.join(route_fail_parts)})" if route_fail_parts else ""
        return (
            f"Route requests: {requests_sent} (dupes: {duplicate_requests}) | "
            f"received: {total} | "
            f"on-time: {self.total_on_time} ({100*self.total_on_time/total:.1f}%) | "
            f"timed-out: {self.total_timed_out} ({100*self.total_timed_out/total:.1f}%) | "
            f"too-late: {self.total_too_late} ({100*self.total_too_late/total:.1f}%) | "
            f"dropped (arrived): {dropped_vehicle_gone} | "
            f"already-at-edge: {already_at_edge or 0} | "
            f"unaccounted: {unaccounted} | "
            f"exceptions: {self.total_exceptions}"
            f"{route_fail_str} | "
            f"poll: {self._poll_ms}ms | step-delay: {self._step_delay_s*1000:.0f}ms"
            + (f" | train-delay: {self._training_delay_s*1000:.0f}ms" if self._training_delay_s > 0 else "")
        )

    @property
    def timeout_rate(self) -> float:
        """Timeout rate over the last window (THROTTLE_WINDOW steps)"""
        on_time   = sum(x[0] for x in self._window)
        timed_out = sum(x[1] for x in self._window)
        total = on_time + timed_out
        return timed_out / total if total > 0 else 0.0

    @property
    def too_late_rate(self) -> float:
        """Too-late rate over the last window (THROTTLE_WINDOW steps)"""
        on_time   = sum(x[0] for x in self._window)
        timed_out = sum(x[1] for x in self._window)
        too_late  = sum(x[2] for x in self._window)
        total = on_time + timed_out + too_late
        return too_late / total if total > 0 else 0.0

    def stats(self) -> dict:
        total = self.total_on_time + self.total_timed_out + self.total_too_late
        return {
            "total": total,
            "on_time": self.total_on_time,
            "timed_out": self.total_timed_out,
            "too_late": self.total_too_late,
            "exceptions": self.total_exceptions,
            "on_time_pct": round(100 * self.total_on_time / total, 1) if total > 0 else None,
            "timed_out_pct": round(100 * self.total_timed_out / total, 1) if total > 0 else None,
            "too_late_pct": round(100 * self.total_too_late / total, 1) if total > 0 else None,
        }

    @property
    def poll_timeout_ms(self) -> int:
        return self._poll_ms
