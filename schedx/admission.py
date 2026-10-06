"""Bounded, process-shared admission for tools submitted through one namespace."""

from __future__ import annotations

import fcntl
import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from schedx.state import atomic_json


PRIORITIES = {"interactive": 0, "test": 1, "compile": 2, "package": 2, "background": 3}


class AdmissionError(RuntimeError):
    """The shared admission contract could not be established."""


class AdmissionTimeout(TimeoutError):
    def __init__(self, waited: float, telemetry: dict):
        super().__init__("tool deadline expired while waiting for admission")
        self.waited = waited
        self.telemetry = telemetry


class _StateLockTimeout(TimeoutError):
    pass


@dataclass(frozen=True)
class AdmissionConfig:
    mode: str = "adaptive"
    initial_limit: int = 4
    min_limit: int = 1
    max_limit: int = 8
    interactive_reserve: int = 1
    max_pending: int = 128
    aging_seconds: float = 2.0
    sample_seconds: float = 1.0
    cooldown_seconds: float = 3.0

    def __post_init__(self) -> None:
        if self.mode not in {"fixed", "adaptive"}:
            raise ValueError("admission mode must be fixed or adaptive")
        for name in ("initial_limit", "min_limit", "max_limit", "max_pending"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4096:
                raise ValueError(f"admission {name} must be an integer in [1, 4096]")
        if not self.min_limit <= self.initial_limit <= self.max_limit:
            raise ValueError("admission requires min_limit <= initial_limit <= max_limit")
        if (isinstance(self.interactive_reserve, bool) or not isinstance(self.interactive_reserve, int)
                or not 0 <= self.interactive_reserve < self.max_limit):
            raise ValueError("interactive_reserve must be nonnegative and smaller than max_limit")
        for name in ("aging_seconds", "sample_seconds", "cooldown_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"admission {name} must be finite and positive")


@dataclass
class AdmissionLease:
    token: str
    fd: int
    submitted_at: float
    admitted_at: float
    telemetry: dict


def pressure_totals(root: Path = Path("/proc/pressure")) -> dict[str, int]:
    """Use cumulative stalls so short windows do not depend on avg10 smoothing."""
    result = {}
    for resource in ("cpu", "memory", "io"):
        try:
            for line in (root / resource).read_text().splitlines():
                if line.startswith("some "):
                    raw = dict(item.split("=", 1) for item in line.split()[1:]).get("total", "")
                    if raw.isdigit():
                        result[resource] = int(raw)
                    break
        except (OSError, ValueError):
            continue
    return result


class ToolAdmission:
    """All cooperating callers must use the same directory and configuration.

    A live file-lock lease is inherited by the launched tool. A populated owned
    cgroup also retains the slot if a descendant closes the inherited descriptor.
    No CPU share or instantaneous-start guarantee is implied by admission order.
    """

    def __init__(self, directory: Path, config: AdmissionConfig | None = None, *,
                 cgroup_root: Path = Path("/sys/fs/cgroup"),
                 pressure_reader: Callable[[], dict[str, int]] = pressure_totals,
                 clock: Callable[[], float] = time.monotonic,
                 poll_seconds: float = 0.01):
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("admission polling interval must be positive")
        self.directory = directory.resolve()
        self.path = self.directory / "state.json"
        self.config = config or AdmissionConfig()
        self.cgroup_root = cgroup_root.resolve()
        self.pressure_reader = pressure_reader
        self.clock = clock
        self.poll_seconds = poll_seconds

    @contextmanager
    def _locked(self, deadline: float | None = None):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.with_suffix(".json.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        cutoff = self.clock() + 5 if deadline is None else deadline
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = cutoff - self.clock()
                    if remaining <= 0:
                        raise _StateLockTimeout("admission state lock deadline expired")
                    time.sleep(min(self.poll_seconds, remaining))
            yield
        finally:
            os.close(fd)

    def _empty(self) -> dict:
        return {"version": 1, "config": asdict(self.config), "cgroup_root": str(self.cgroup_root),
                "limit": self.config.initial_limit, "sequence": 0, "jobs": {}, "last_served": {},
                "control": {"reason": "initial", "high_windows": 0, "low_windows": 0},
                "completions": [], "reaped": 0}

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            state = json.loads(self.path.read_text())
            if (not isinstance(state, dict) or state.get("version") != 1
                    or not isinstance(state.get("jobs"), dict)
                    or not isinstance(state.get("control"), dict)
                    or not isinstance(state.get("last_served"), dict)
                    or not isinstance(state.get("completions"), list)
                    or not isinstance(state.get("sequence"), int)
                    or not isinstance(state.get("limit"), int)
                    or not isinstance(state.get("reaped"), int)
                    or not isinstance(state.get("config"), dict)
                    or not isinstance(state.get("cgroup_root"), str)):
                raise ValueError("invalid state schema")
            for token, job in state["jobs"].items():
                if (len(token) != 32 or any(ch not in "0123456789abcdef" for ch in token)
                        or not isinstance(job, dict) or job.get("status") not in {"queued", "running"}
                        or job.get("intent") not in PRIORITIES
                        or not isinstance(job.get("agent"), str)
                        or not all(isinstance(job.get(key), (int, float)) and not isinstance(job.get(key), bool)
                                   and math.isfinite(job[key]) for key in ("submitted", "deadline", "sequence"))):
                    raise ValueError("invalid job record")
            for row in state["completions"]:
                if (not isinstance(row, dict) or row.get("intent") not in PRIORITIES
                        or not isinstance(row.get("execution_timeout"), bool)
                        or not all(isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool)
                                   and math.isfinite(row[key]) and row[key] >= 0 for key in ("at", "end_to_end_seconds"))):
                    raise ValueError("invalid completion record")
            sample = state["control"].get("sample")
            if sample is not None and (not isinstance(sample, dict) or not isinstance(sample.get("at"), (int, float))
                    or not math.isfinite(sample["at"]) or not isinstance(sample.get("totals"), dict)):
                raise ValueError("invalid pressure sample")
            return state
        except (OSError, ValueError, TypeError) as exc:
            raise AdmissionError(f"admission state could not be read: {exc}") from exc

    def _lease_path(self, token: str) -> Path:
        return self.directory / (token + ".lease")

    def _lease_live(self, token: str) -> bool:
        try:
            fd = os.open(self._lease_path(token), os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
        finally:
            os.close(fd)

    def _populated(self, job: dict) -> bool:
        raw = job.get("cgroup")
        if not raw:
            return False
        group = Path(raw)
        if not group.is_absolute() or self.cgroup_root / "schedx-agents" not in group.parents:
            raise AdmissionError("admission cgroup is outside the managed subtree")
        try:
            if group.stat().st_ino != job.get("cgroup_id"):
                # The original group is gone; the replacement belongs to another run.
                return False
            events = group / "cgroup.events"
            if events.exists():
                values = dict(line.split() for line in events.read_text().splitlines())
                if values.get("populated") not in {"0", "1"}:
                    raise AdmissionError("cannot verify cgroup population")
                return values["populated"] == "1"
            return bool((group / "cgroup.procs").read_text().strip())
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as exc:
            raise AdmissionError(f"cannot verify admission workload cleanup: {exc}") from exc

    def _reap(self, state: dict, now: float) -> None:
        for token, job in list(state["jobs"].items()):
            expired_wait = job["status"] == "queued" and job["deadline"] <= now
            if expired_wait or (not self._lease_live(token) and not self._populated(job)):
                del state["jobs"][token]
                self._lease_path(token).unlink(missing_ok=True)
                state["reaped"] += 1

    def _validate_config(self, state: dict) -> dict:
        if state["config"] != asdict(self.config) or state["cgroup_root"] != str(self.cgroup_root):
            if state["jobs"]:
                raise AdmissionError("active callers in this admission namespace use a different configuration")
            return self._empty()
        if not self.config.min_limit <= state["limit"] <= self.config.max_limit:
            raise AdmissionError("stored admission limit is outside the configured bounds")
        return state

    def _adapt(self, state: dict, now: float) -> None:
        if self.config.mode == "fixed":
            return
        control = state["control"]
        previous = control.get("sample")
        if previous and now - previous["at"] < self.config.sample_seconds:
            return
        raw = self.pressure_reader()
        valid = {key: value for key, value in raw.items() if key in {"cpu", "memory", "io"}
                 and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
        control["sample"] = {"at": now, "totals": valid}
        if not previous or now <= previous["at"]:
            control.update(reason="pressure_warmup", high_windows=0, low_windows=0)
            return
        elapsed = (now - previous["at"]) * 1_000_000
        old = previous["totals"]
        pressure = {key: min(100.0, 100 * (value - old[key]) / elapsed)
                    for key, value in valid.items() if key in old and value >= old[key]}
        control["pressure_percent"] = pressure
        # Missing telemetry cannot certify low pressure and must not expand capacity.
        if set(pressure) != {"cpu", "memory", "io"}:
            control.update(reason="pressure_unavailable", high_windows=0, low_windows=0)
            return
        high = pressure["cpu"] >= 20 or pressure["memory"] >= 10 or pressure["io"] >= 10
        low = pressure["cpu"] < 5 and pressure["memory"] < 1 and pressure["io"] < 1
        control["high_windows"] = control.get("high_windows", 0) + 1 if high else 0
        control["low_windows"] = control.get("low_windows", 0) + 1 if low else 0
        recent = [row for row in state["completions"][-8:] if now - row["at"] <= 30]
        execution_timeouts = sum(row["execution_timeout"] for row in recent)
        control["recent_execution_timeouts"] = execution_timeouts
        if now - control.get("changed_at", -math.inf) < self.config.cooldown_seconds:
            control["reason"] = "cooldown"
            return
        old_limit = state["limit"]
        if control["high_windows"] >= 2:
            state["limit"] = max(self.config.min_limit, old_limit // 2)
            control["reason"] = "pressure_high"
        elif (control["low_windows"] >= 3 and not execution_timeouts
              and any(job["status"] == "queued" for job in state["jobs"].values())):
            state["limit"] = min(self.config.max_limit, old_limit + 1)
            control["reason"] = "healthy_demand"
        else:
            control["reason"] = "hold"
        if state["limit"] != old_limit:
            control.update(changed_at=now, high_windows=0, low_windows=0)

    def _promote(self, state: dict, now: float) -> None:
        active = [job for job in state["jobs"].values() if job["status"] == "running"]
        while len(active) < state["limit"]:
            ordinary = sum(job["intent"] != "interactive" for job in active)
            ordinary_cap = max(1, state["limit"] - self.config.interactive_reserve)
            candidates = [(token, job) for token, job in state["jobs"].items()
                          if job["status"] == "queued" and job["deadline"] > now
                          and (job["intent"] == "interactive" or ordinary < ordinary_cap)]
            if not candidates:
                break
            aged = [(token, job) for token, job in candidates if now - job["submitted"] >= self.config.aging_seconds]
            if aged:
                token, job = min(aged, key=lambda row: row[1]["sequence"])
            else:
                token, job = min(candidates, key=lambda row: (PRIORITIES[row[1]["intent"]],
                    state["last_served"].get(row[1]["agent"], -1), row[1]["sequence"]))
            state["sequence"] += 1
            state["last_served"][job["agent"]] = state["sequence"]
            job.update(status="running", admitted=now)
            active.append(job)
        agents = {job["agent"] for job in state["jobs"].values()}
        state["last_served"] = {agent: value for agent, value in state["last_served"].items() if agent in agents}

    @staticmethod
    def _telemetry(state: dict) -> dict:
        return {"mode": state["config"]["mode"], "limit": state["limit"],
                "active": sum(job["status"] == "running" for job in state["jobs"].values()),
                "pending": sum(job["status"] == "queued" for job in state["jobs"].values()),
                "reaped": state["reaped"], "control": dict(state["control"])}

    def acquire(self, agent_id: str, intent: str, *, submitted_at: float, deadline: float) -> AdmissionLease:
        if intent not in PRIORITIES or not isinstance(agent_id, str) or not agent_id:
            raise ValueError("admission requires a named agent and supported intent")
        if not all(math.isfinite(value) for value in (submitted_at, deadline)) or deadline <= submitted_at:
            raise ValueError("admission deadline must be finite and after submission")
        self.directory.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        fd = os.open(self._lease_path(token), os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        registered = False
        observed_version = None
        refreshed_at = -math.inf
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while True:
                now = self.clock()
                if registered and now < deadline and now - refreshed_at < min(.2, self.config.sample_seconds):
                    current = self.path.stat()
                    version = (current.st_ino, current.st_mtime_ns, current.st_size)
                    if version == observed_version:
                        # Pending callers need not all parse and lock the same unchanged
                        # registry. Periodic refresh still recovers a caller that died
                        # without updating it; writes wake waiters on their next poll.
                        time.sleep(min(self.poll_seconds, max(0, deadline - now)))
                        continue
                with self._locked(deadline):
                    now = self.clock()
                    state = self._load()
                    before = json.dumps(state, sort_keys=True)
                    self._reap(state, now)
                    state = self._validate_config(state)
                    if now >= deadline:
                        state["jobs"].pop(token, None)
                        atomic_json(self.path, state)
                        raise AdmissionTimeout(now - submitted_at, self._telemetry(state))
                    if not registered:
                        if sum(job["status"] == "queued" for job in state["jobs"].values()) >= self.config.max_pending:
                            raise AdmissionError("admission queue is full; command was not started")
                        state["sequence"] += 1
                        state["jobs"][token] = {"agent": agent_id, "intent": intent, "status": "queued",
                            "submitted": submitted_at, "deadline": deadline, "sequence": state["sequence"]}
                        registered = True
                    self._adapt(state, now)
                    self._promote(state, now)
                    if before != json.dumps(state, sort_keys=True):
                        atomic_json(self.path, state)
                    current = self.path.stat()
                    observed_version = (current.st_ino, current.st_mtime_ns, current.st_size)
                    refreshed_at = now
                    job = state["jobs"][token]
                    if job["status"] == "running":
                        return AdmissionLease(token, fd, submitted_at, job["admitted"], self._telemetry(state))
                time.sleep(min(self.poll_seconds, max(0, deadline - self.clock())))
        except BaseException as exc:
            try:
                if registered:
                    try:
                        with self._locked(self.clock() + 0.1):
                            state = self._load()
                            state["jobs"].pop(token, None)
                            atomic_json(self.path, state)
                    except _StateLockTimeout:
                        # Closing the unique lease allows recovery when the lock is available.
                        pass
            finally:
                os.close(fd)
                self._lease_path(token).unlink(missing_ok=True)
            if isinstance(exc, _StateLockTimeout):
                raise AdmissionTimeout(self.clock() - submitted_at,
                    {"mode": self.config.mode, "limit": None, "control": {"reason": "state_lock_timeout"}}) from exc
            raise

    def bind_cgroup(self, lease: AdmissionLease, group: Path) -> None:
        group = group.resolve()
        if self.cgroup_root / "schedx-agents" not in group.parents:
            raise AdmissionError("tool group must be inside the managed subtree")
        try:
            with self._locked():
                state = self._load()
                job = state["jobs"].get(lease.token)
                if not job or job["status"] != "running":
                    raise AdmissionError("admission lease is no longer registered")
                job.update(cgroup=str(group), cgroup_id=group.stat().st_ino)
                atomic_json(self.path, state)
        except _StateLockTimeout as exc:
            raise AdmissionError(str(exc)) from exc

    def release(self, lease: AdmissionLease, result: dict | None = None) -> bool:
        if lease.fd < 0:
            return True
        removed = False
        try:
            with self._locked():
                state = self._load()
                job = state["jobs"].get(lease.token)
                if job is None or not self._populated(job):
                    state["jobs"].pop(lease.token, None)
                    removed = True
                    if result is not None:
                        state["completions"].append({"intent": result["intent"],
                            "at": self.clock(),
                            "end_to_end_seconds": result["duration_seconds"],
                            "execution_timeout": bool(result.get("timed_out") and result.get("command_started"))})
                        state["completions"] = state["completions"][-64:]
                self._reap(state, self.clock())
                self._promote(state, self.clock())
                atomic_json(self.path, state)
        except _StateLockTimeout as exc:
            raise AdmissionError(str(exc)) from exc
        finally:
            os.close(lease.fd)
            lease.fd = -1
            if removed:
                self._lease_path(lease.token).unlink(missing_ok=True)
        return removed

    def snapshot(self) -> dict:
        with self._locked():
            state = self._load()
            before = json.dumps(state, sort_keys=True)
            self._reap(state, self.clock())
            state = self._validate_config(state)
            self._adapt(state, self.clock())
            self._promote(state, self.clock())
            if before != json.dumps(state, sort_keys=True):
                atomic_json(self.path, state)
            return {**self._telemetry(state), "jobs": dict(state["jobs"])}
