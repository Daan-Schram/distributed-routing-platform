"""Simulated Annealing tuner for a learner's hyperparameters"""

import copy
import json
import math
import random as _rand
import threading
import time
import uuid as _uuid_mod
from datetime import datetime as _dt
from pathlib import Path

import algorithms


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _searchable(algo) -> dict:
    tunables = algorithms.PLATFORM_TUNABLES if algo.needs_snapshot else ()
    return {p.name: p for p in (*algo.params, *tunables) if p.search is not None}


def _build_space(algo, param_space: list) -> dict:
    """Turn the request's [{key, min, max}, ...] into {name: (param, lo, hi)}"""
    allowed = _searchable(algo)
    space: dict = {}
    for entry in param_space or []:
        param = allowed.get(entry.get("key"))
        if not param:
            continue
        if param.type not in ("int", "float"):
            space[param.name] = (param, None, None)
            continue
        try:
            lo = float(entry.get("min", param.search[0]))
            hi = float(entry.get("max", param.search[1]))
        except (TypeError, ValueError):
            lo, hi = param.search
        if lo > hi:
            lo, hi = hi, lo
        if param.bounds:
            lo = _clamp(lo, *param.bounds)
            hi = _clamp(hi, *param.bounds)
        space[param.name] = (param, lo, hi)
    return space


def _start_value(param, lo, hi):
    """Start point for a searched parameter: the backend default if it falls
    inside the user's range, otherwise the midpoint of the range"""
    if lo is None:
        return param.default
    d = param.default
    if lo <= d <= hi:
        v = float(d)
    elif param.scale == "log" and lo > 0 and hi > 0:
        v = math.sqrt(lo * hi)
    else:
        v = (lo + hi) / 2.0
    return int(round(v)) if param.type == "int" else v


def _split(values: dict) -> tuple[dict, dict]:
    platform = {p.name for p in algorithms.PLATFORM_TUNABLES}
    return ({k: v for k, v in values.items() if k in platform},
            {k: v for k, v in values.items() if k not in platform})


def _fixed(cfg: dict, space: dict) -> tuple[dict, dict]:
    return _split({k: v for k, v in (cfg.get("fixed") or {}).items() if k not in space})


def _random_neighbor(current: dict, space: dict, sigma: float, n_perturb: int) -> dict:
    neighbor = copy.deepcopy(current)
    if not space:
        return neighbor
    keys = _rand.sample(list(space.keys()), k=min(n_perturb, len(space)))
    for key in keys:
        param, lo, hi = space[key]
        if param.type == "bool":
            neighbor[key] = not bool(current.get(key, True))
            continue
        if param.type == "choice":
            others = [c for c in param.choices if c != current.get(key)]
            neighbor[key] = _rand.choice(others) if others else current.get(key)
            continue
        v = current[key]
        if param.scale == "log":
            v = _clamp(v * math.exp(_rand.gauss(0, sigma)), lo, hi)
        else:  # linear
            v = _clamp(v + _rand.gauss(0, sigma * (hi - lo) * 0.25), lo, hi)
        neighbor[key] = int(round(v)) if param.type == "int" else v
    return neighbor


def _censored_att(d: dict):
    """ATT with vehicles that never arrived scored at a lower bound on their travel time.

    Discarding timed-out runs makes a looping policy unscoreable
    """
    att = d.get("att") or {}
    arrived = att.get("count", 0)
    mean = att.get("mean")
    departures = d.get("departures") or arrived
    sim_duration = d.get("sim_duration_s") or 0.0

    if arrived == 0 or mean is None:
        return sim_duration or None

    missing = max(0, departures - arrived)
    if not missing:
        return mean
    censor = max(sim_duration, mean * 2.0)
    return (mean * arrived + censor * missing) / (arrived + missing)


def _objective(results: list, score_mode: str = "mean"):
    if not results:
        return None, "no_result"
    vals = [v for v in (_censored_att(d) for d in results) if v is not None]
    if not vals:
        return None, "too_few"
    if score_mode == "last":
        last = _censored_att(results[-1])
        return (last, "ok") if last is not None else (None, "last_missing")
    if score_mode == "mean_after_first" and len(vals) > 1:
        vals = vals[1:]
    return sum(vals) / len(vals), "ok"


class SimulatedAnnealingTuner:
    """Drives an SA search over a learner's hyperparameters via the experiment queue."""

    def __init__(self, metrics_dir, state_dir, experiment_queue, queue_lock,
                 kill_running_experiment):
        self.metrics_dir = Path(metrics_dir)
        self.state_file = Path(state_dir) / "sa_state.json"
        self.experiment_queue = experiment_queue
        self.queue_lock = queue_lock
        self._kill_running_experiment = kill_running_experiment

        self.state: dict = {"status": "idle"}
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self._search_active = False

    @property
    def search_active(self) -> bool:
        return self._search_active

    def validate(self, cfg: dict) -> str | None:
        """Reason a config cannot be searched"""
        try:
            algo = algorithms.get(cfg.get("algorithm"))
        except ValueError as e:
            return str(e)
        if not algo.learns:
            return f"{algo.label} does not learn, there is nothing to search."
        repeats = max(1, int(cfg.get("repeats", 1)))
        if bool(cfg.get("warm_start", False)) and repeats < 2:
            return "Warm start needs repeats >= 2 — repeat 1 trains, repeat 2 reuses its weights."
        space = _build_space(algo, cfg.get("param_space", []))
        if not space:
            return "No parameters selected to search."
        try:
            algo.resolve(_fixed(cfg, space)[1])
        except ValueError as e:
            return str(e)
        if cfg.get("resume") and self.state_file.exists():
            try:
                saved = json.loads(self.state_file.read_text()).get("algorithm")
            except Exception:
                saved = None
            if saved != algo.name:
                return f"The saved search is for {saved}, not {algo.name}."
        if int(cfg.get("jam_top_edges", 0)) > 0 and float(cfg.get("jam_factor", 12.0)) <= 1.0:
            return "Jam factor must be > 1.0 when a bottleneck is requested."
        return None

    def start(self, cfg: dict) -> bool:
        """Begin a search. Returns False if one is already running."""
        with self.lock:
            if self.state.get("status") == "running":
                return False
        self.stop_event.clear()
        with self.lock:
            self.state.update({"status": "starting", "message": "Starting SA search..."})
        self.thread = threading.Thread(target=self._loop, args=(cfg,), daemon=True)
        self.thread.start()
        return True

    def cancel(self) -> None:
        """Signal an active search to stop after the current experiment"""
        if self.state.get("status") not in ("running", "starting", "stopping"):
            return
        self.stop_event.set()
        self._search_active = False
        with self.lock:
            if self.state.get("status") in ("running", "starting"):
                self.state["status"] = "stopping"
                self.state["message"] = "Stopping after current experiment..."

    def discard(self, group_id: str | None) -> bool:
        """Delete an entire SA search: cancel the loop, remove all its queue
        items (queued and done), and kill the running experiment"""
        self.cancel()
        if not group_id:
            return False
        with self.queue_lock:
            need_kill = any(i.get("group_id") == group_id and i["status"] == "running"
                            for i in self.experiment_queue)
            self.experiment_queue[:] = [i for i in self.experiment_queue
                                        if i.get("group_id") != group_id]
        if need_kill:
            self._kill_running_experiment()
        return need_kill

    def status(self) -> dict:
        with self.lock:
            return dict(self.state)

    def has_state_file(self) -> bool:
        return self.state_file.exists()

    def _collect_results(self, item_id: str) -> list:
        """Run JSONs of one queue item in repeat order"""
        out = []
        for path in self.metrics_dir.glob("*.json"):
            try:
                with open(path) as f:
                    data = json.load(f)
                cfg = data.get("config", {})
                if cfg.get("queue_item_id") == item_id:
                    out.append((cfg.get("repeat_index", 0), path.stat().st_mtime, data))
            except Exception:
                pass
        return [d for _, _, d in sorted(out, key=lambda t: (t[0], t[1]))]

    def _save_state(self, state: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            tmp.replace(self.state_file)
        except Exception:
            pass

    def _annotate_results(self, item_id: str, sa_block: dict) -> None:
        for path in self.metrics_dir.glob("*.json"):
            try:
                with open(path) as f:
                    data = json.load(f)
                if data.get("config", {}).get("queue_item_id") != item_id:
                    continue
                data["sa"] = sa_block
                tmp = path.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(data, f, indent=2)
                tmp.replace(path)
            except Exception:
                pass

    def _loop(self, cfg: dict) -> None:
        n_iter = int(cfg.get("n_iter", 80))
        t_init = float(cfg.get("t_init", 800.0))
        alpha = float(cfg.get("alpha", 0.97))
        sigma = float(cfg.get("sigma", 0.4))
        n_perturb = int(cfg.get("n_perturb", 1))
        max_rt = int(cfg.get("max_runtime_s", 900))
        repeats = max(1, int(cfg.get("repeats", 1)))
        resume = bool(cfg.get("resume", False))
        warm_start = bool(cfg.get("warm_start", False))
        score_mode = cfg.get("score_mode") or ("last" if warm_start else "mean")

        algo = algorithms.get(cfg["algorithm"])
        space = _build_space(algo, cfg.get("param_space", []))
        fixed_platform, fixed_hp = _fixed(cfg, space)

        self._search_active = True

        base = {p.name: fixed_platform.get(p.name, p.default) for p in algorithms.PLATFORM_TUNABLES}
        base.update({
            "algorithm":                  algo.name,
            "network_name":               cfg.get("network_name", "irregular_6x6_short_and_calm"),
            "num_workers":                int(cfg.get("num_workers", 2)),
            "congestion_speed_threshold": 1,
            "broadcast_rate":             int(cfg.get("broadcast_rate", 50)),
            "stats_print_interval":       int(cfg.get("stats_print_interval", 100)),
            "use_gui":                    bool(cfg.get("use_gui", False)),
            "capture_trajectories":       bool(cfg.get("capture_trajectories", False)),
            "sumo_seed":                  cfg.get("sumo_seed"),
            "telemetry":                  bool(cfg.get("telemetry", True)),
            "log_proxies":                cfg.get("log_proxies"),
            "jam_top_edges":              int(cfg.get("jam_top_edges", 0)) if algo.bottleneck else 0,
            "jam_factor":                 float(cfg.get("jam_factor", 12.0)),
            "max_runtime_s":              max_rt,
        })

        group_id = f"sa_{_dt.now().strftime('%Y%m%d_%H%M%S')}_{_uuid_mod.uuid4().hex[:4]}"

        def _initial_solution() -> dict:
            return {k: _start_value(*sp) for k, sp in space.items()}

        resume_ok = False
        if resume and self.state_file.exists():
            try:
                saved = json.loads(self.state_file.read_text())
                current_sol = saved["current_solution"]
                current_att = saved["current_att"]
                best_sol    = saved["best_solution"]
                best_att    = saved["best_att"]
                start_iter  = saved["iteration"] + 1
                history     = saved.get("history", [])
                group_id    = saved.get("group_id", group_id)
                init = _initial_solution()
                current_sol = {k: current_sol.get(k, init[k]) for k in space}
                best_sol    = {k: best_sol.get(k, init[k]) for k in space}
                resume_ok   = True
            except Exception:
                pass

        if not resume_ok:
            current_sol = _initial_solution()
            current_att = float("inf")
            best_sol    = dict(current_sol)
            best_att    = float("inf")
            start_iter  = 0
            history     = []

        def _upd(**kw):
            with self.lock:
                self.state.update(kw)

        _upd(
            status="running",
            algorithm=algo.name,
            iteration=start_iter,
            n_iter=n_iter,
            start_iter=start_iter,
            group_id=group_id,
            repeats=repeats,
            warm_start=warm_start,
            score_mode=score_mode,
            max_runtime_s=max_rt,
            jam_top_edges=base["jam_top_edges"],
            jam_factor=base["jam_factor"],
            capture_trajectories=base["capture_trajectories"],
            t_init=t_init,
            alpha=alpha,
            temperature=round(t_init * (alpha ** start_iter), 2),
            current_att=None if current_att == float("inf") else current_att,
            best_att=None if best_att == float("inf") else best_att,
            best_solution=best_sol,
            history=history[-20:],
            message="Starting...",
        )

        def _submit_item(sol: dict) -> str:
            platform, hp = _split(sol)
            item = {
                "id": str(_uuid_mod.uuid4()),
                "config": {**base, **platform, "hyperparams": algo.resolve({**fixed_hp, **hp}), "group_id": group_id},
                "repeats": repeats,
                "repeat_done": 0,
                "load_checkpoint_first": False,
                "save_checkpoint": warm_start,
                "reset_checkpoint_first": warm_start,
                "status": "queued",
                "group_id": group_id,
            }
            with self.queue_lock:
                self.experiment_queue.append(item)
            return item["id"]

        def _wait_item(item_id: str) -> str:
            """'done' | 'error' | 'stopped'"""
            while not self.stop_event.is_set():
                with self.queue_lock:
                    match = next((i for i in self.experiment_queue if i["id"] == item_id), None)
                if match is None or match["status"] == "done":
                    return "done"
                if match["status"] == "error":
                    return "error"
                time.sleep(3)
            return "stopped"

        sa_settings = {"t_init": t_init, "alpha": alpha, "n_iter": n_iter, "repeats": repeats,
                       "warm_start": warm_start, "score_mode": score_mode}

        def _evaluate(sol: dict):
            iid = _submit_item(sol)
            outcome = _wait_item(iid)
            if outcome == "stopped" or self.stop_event.is_set():
                return None, True, "stopped", iid
            if outcome == "error":
                return None, False, "error", iid
            att, reason = _objective(self._collect_results(iid), score_mode)
            return att, False, reason, iid

        first_neighbor = 1 if not resume_ok else start_iter
        _upd(runs_total=((0 if resume_ok else 1) + (n_iter - first_neighbor) + 1) * repeats, confirming=False)
        if not resume_ok:
            _upd(message="Initial candidate queued — press Start queue to run.")
            att, stopped, reason, iid = _evaluate(current_sol)
            if stopped:
                self._search_active = False
                _upd(status="stopped", message="Stopped before initial evaluation completed.")
                return
            self._annotate_results(iid, {
                "iteration": 0, "temperature": round(t_init, 2),
                "att": round(att, 1) if att is not None else None,
                "reason": reason, "accepted": att is not None,
                "delta": 0.0, "prob": 1.0, "solution": dict(current_sol),
                **sa_settings,
            })
            if att is not None:
                current_att = att
                best_att = att
                best_sol = dict(current_sol)
                history.append({"iteration": 0, "solution": dict(current_sol),
                                "att": round(att, 1), "accepted": True, "temperature": round(t_init, 2)})
                self._save_state({"algorithm": algo.name, "iteration": 0, "group_id": group_id, "current_solution": current_sol,
                                  "current_att": current_att, "best_solution": best_sol,
                                  "best_att": best_att, "history": history})
            _init_invalid = {"too_few": "Initial run completed but <20 vehicles arrived — relax the ranges.",
                             "no_result": "Initial run produced no result JSON — check the run logs.",
                             "last_missing": "Initial run's last repeat has no ATT — check the run logs.",
                             "error": "Initial run failed (see the queue item) — check the run logs."}
            _upd(current_att=round(current_att, 1) if current_att != float("inf") else None,
                 best_att=round(best_att, 1) if best_att != float("inf") else None,
                 best_solution=best_sol, history=history[-20:],
                 message=(f"Initial ATT: {current_att:.0f}s — starting search." if current_att != float("inf")
                          else _init_invalid.get(reason, "Initial evaluation invalid.")))

        for global_iter in range(first_neighbor, n_iter):
            if self.stop_event.is_set():
                break

            T = t_init * (alpha ** global_iter)
            candidate = _random_neighbor(current_sol, space, sigma, n_perturb)

            _upd(iteration=global_iter, temperature=round(T, 2),
                 message=f"Iter {global_iter + 1}/{n_iter} — waiting for simulation...")

            new_att, stopped, reason, iid = _evaluate(candidate)
            if stopped:
                break
            if new_att is None:
                reason_msg = {"too_few": "<20 vehicles arrived", "no_result": "no result",
                              "last_missing": "last repeat has no ATT", "error": "run failed"}.get(reason, "invalid")
                history.append({"iteration": global_iter, "solution": candidate,
                                "att": None, "reason": reason, "accepted": False, "temperature": round(T, 2)})
                self._annotate_results(iid, {
                    "iteration": global_iter, "temperature": round(T, 2),
                    "att": None, "reason": reason, "accepted": False,
                    "delta": None, "prob": None, "solution": candidate,
                    **sa_settings,
                })
                _upd(iteration=global_iter, temperature=round(T, 2), history=history[-20:],
                     message=f"Iter {global_iter + 1}: {reason_msg} — rejected.")
                self._save_state({"algorithm": algo.name, "iteration": global_iter, "group_id": group_id, "current_solution": current_sol,
                                  "current_att": current_att, "best_solution": best_sol,
                                  "best_att": best_att, "history": history})
                continue

            delta = new_att - current_att if current_att != float("inf") else 0.0
            prob = 1.0 if delta <= 0 else (math.exp(-delta / T) if T > 0 else 0.0)
            accepted = _rand.random() < prob
            new_best = new_att < best_att

            if accepted:
                current_sol = candidate
                current_att = new_att
            if new_best:
                best_sol = dict(candidate)
                best_att = new_att

            self._annotate_results(iid, {
                "iteration": global_iter, "temperature": round(T, 2),
                "att": round(new_att, 1), "reason": "ok", "accepted": accepted,
                "delta": round(delta, 1), "prob": round(prob, 4), "solution": candidate,
                **sa_settings,
            })

            tag = "ACCEPT" if accepted else "REJECT"
            suffix = "  BEST" if new_best else ""
            msg = f"Iter {global_iter + 1}: ATT={new_att:.0f}s  Δ={delta:+.0f}  P={prob:.2f}  [{tag}]{suffix}"

            history.append({"iteration": global_iter, "solution": candidate,
                            "att": round(new_att, 1), "accepted": accepted, "temperature": round(T, 2)})
            self._save_state({"algorithm": algo.name, "iteration": global_iter, "group_id": group_id, "current_solution": current_sol,
                              "current_att": current_att, "best_solution": best_sol,
                              "best_att": best_att, "history": history})

            _upd(iteration=global_iter, temperature=round(T, 2),
                 current_att=round(current_att, 1) if current_att != float("inf") else None,
                 best_att=round(best_att, 1) if best_att != float("inf") else None,
                 best_solution=best_sol, history=history[-20:], message=msg)

        # Re-evaluate the best once to confirm it wasn't lucky
        best_confirmed = None
        if best_att != float("inf") and not self.stop_event.is_set():
            _upd(message="Re-evaluating best solution to confirm...", confirming=True)
            best_confirmed, _, c_reason, c_iid = _evaluate(best_sol)
            self._annotate_results(c_iid, {
                "iteration": n_iter - 1, "temperature": round(t_init * (alpha ** (n_iter - 1)), 2),
                "att": round(best_confirmed, 1) if best_confirmed is not None else None,
                "reason": c_reason, "accepted": False, "confirm": True,
                "delta": None, "prob": None, "solution": dict(best_sol),
                **sa_settings,
            })
            self._save_state({"algorithm": algo.name, "iteration": n_iter - 1, "group_id": group_id, "current_solution": current_sol,
                              "current_att": current_att, "best_solution": best_sol,
                              "best_att": best_att, "best_att_confirmed": best_confirmed, "history": history})

        self._search_active = False
        final_status = "done" if not self.stop_event.is_set() else "stopped"
        if best_att == float("inf"):
            final_msg = "Done — no valid evaluation (every run timed out)."
        elif best_confirmed is not None:
            final_msg = f"Done. Best ATT (search): {best_att:.0f}s · confirmed re-eval: {best_confirmed:.0f}s"
        else:
            final_msg = f"Done after {n_iter} iterations. Best ATT: {best_att:.0f}s"
        _upd(status=final_status, message=final_msg,
             best_att_confirmed=round(best_confirmed, 1) if best_confirmed is not None else None)
