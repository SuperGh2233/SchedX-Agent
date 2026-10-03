from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any
from schedx.state import process_start_time

from schedx.controllers.scx_controller import (
    SCX_FAIRNESS_BACKGROUND_MAX,
    SCX_FAIRNESS_BACKGROUND_MIN,
    SCX_FAIRNESS_BACKGROUND_DEFAULT,
    SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL,
    ScxController,
)

DEFAULT_SOCKET = Path("/run/schedx/scx-daemon.sock")


class ScxDaemonClient:
    """Small JSON-line client for the persistent sched_ext owner."""

    def __init__(self, socket_path: Path = DEFAULT_SOCKET, timeout: float = 2.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def request(self, action: str, **parameters: Any) -> dict[str, Any]:
        payload = json.dumps({"action": action, **parameters}).encode() + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            client.connect(str(self.socket_path))
            client.sendall(payload)
            response = b""
            while not response.endswith(b"\n"):
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
        result = json.loads(response.decode())
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error", "scx daemon request failed")))
        return result

    def is_available(self) -> bool:
        try:
            return bool(self.request("status").get("scheduler_running"))
        except (OSError, RuntimeError, json.JSONDecodeError, AttributeError):
            return False

    def set_task_policy(self, pid: int, class_id: int, weight: int, pid_start: str | None = None) -> bool:
        return bool(
            self.request("set_task", pid=pid, class_id=class_id, weight=weight, pid_start=pid_start or process_start_time(pid)).get("updated")
        )

    def remove_task_policy(self, pid: int) -> bool:
        return bool(self.request("remove_task", pid=pid).get("removed"))

    def set_cgroup_policy(self, cgroup_id: int, class_id: int, weight: int, cgroup_path: str | None = None) -> bool:
        return bool(
            self.request(
                "set_cgroup", cgroup_id=cgroup_id, class_id=class_id, weight=weight, cgroup_path=cgroup_path
            ).get("updated")
        )

    def remove_cgroup_policy(self, cgroup_id: int) -> bool:
        return bool(self.request("remove_cgroup", cgroup_id=cgroup_id).get("removed"))

    def cgroup_metrics(self) -> dict[str, dict[str, int]]:
        return self.request("cgroup_metrics")["cgroup_metrics"]


class _ScxDaemonDispatch:
    daemon_threads = True

    def __init__(self, socket_path: Path, controller: ScxController) -> None:
        self.controller = controller
        self.controller_lock = threading.Lock()
        self.reaped_policies = 0
        self.reaped_metrics = 0
        self._next_reap = time.monotonic() + 5
        self.fairness = {
            "mode": "startup",
            "background_interval": getattr(
                controller, "background_interval", SCX_FAIRNESS_BACKGROUND_DEFAULT
            ),
            "default_interval": SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL,
        }
        self._last_metrics: dict[int, dict[str, int]] = {}
        self.cgroup_paths: dict[int, Path] = {}
        self.task_starts: dict[int, str] = {}
        self.control_telemetry: dict[str, Any] = {}
        self.target_background_share = {"low": 0.15, "high": 0.25}
        super().__init__(str(socket_path), _ScxRequestHandler)

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        with self.controller_lock:
            if action == "status":
                return {
                    "ok": True,
                    "scheduler_running": self.controller._process is not None
                    and self.controller._process.poll() is None
                    and self.controller.status().get("state") == "enabled",
                    "sched_ext": self.controller.status(),
                    "reaped_policies": self.reaped_policies,
                    "reaped_metrics": self.reaped_metrics,
                    "fairness": self.fairness,
                    "control_telemetry": self.control_telemetry,
                    "target_background_share": self.target_background_share,
                }
            if action == "set_task":
                pid = int(request["pid"])
                start = request.get("pid_start")
                if start and process_start_time(pid) != start:
                    raise ProcessLookupError("task identity changed before policy registration")
                updated = self.controller.set_task_policy(
                    int(request["pid"]), int(request["class_id"]), int(request["weight"])
                )
                if updated and start:
                    self.task_starts[pid] = start
                return {"ok": True, "updated": updated}
            if action == "remove_task":
                removed = self.controller.remove_task_policy(int(request["pid"]))
                if removed:
                    self.task_starts.pop(int(request["pid"]), None)
                return {"ok": True, "removed": removed}
            if action == "set_cgroup":
                cgroup_path = request.get("cgroup_path")
                if cgroup_path:
                    path = Path(cgroup_path).resolve(strict=True)
                    if Path("/sys/fs/cgroup/schedx-agents") not in path.parents or path.stat().st_ino != int(request["cgroup_id"]):
                        raise ValueError("tool cgroup identity does not match the managed subtree")
                updated = self.controller.set_cgroup_policy(
                    int(request["cgroup_id"]),
                    int(request["class_id"]),
                    int(request["weight"]),
                )
                if updated and cgroup_path:
                    self.cgroup_paths[int(request["cgroup_id"])] = path
                return {"ok": True, "updated": updated}
            if action == "remove_cgroup":
                removed = self.controller.remove_cgroup_policy(int(request["cgroup_id"]))
                if removed:
                    self.cgroup_paths.pop(int(request["cgroup_id"]), None)
                return {"ok": True, "removed": removed}
            if action == "set_fairness":
                background = int(request["background_interval"])
                default = int(request["default_interval"])
                updated = self.controller.set_fairness(background, default)
                self.fairness = {
                    "mode": "manual",
                    "background_interval": background,
                    "default_interval": default,
                }
                return {"ok": True, "updated": updated, "fairness": self.fairness}
            if action == "set_target":
                low = float(request["low"])
                high = float(request["high"])
                if not 0 <= low < high <= 1:
                    raise ValueError("target share must satisfy 0 <= low < high <= 1")
                self.target_background_share = {"low": low, "high": high}
                if self.control_telemetry:
                    self.control_telemetry = {
                        **self.control_telemetry,
                        "target_low": low,
                        "target_high": high,
                    }
                return {"ok": True, "target_background_share": self.target_background_share}
            if action == "stats":
                return {"ok": True, "stats": self.controller.get_stats().to_dict()}
            if action == "class_metrics":
                return {"ok": True, "class_metrics": self.controller.get_class_metrics()}
            if action == "cgroup_metrics":
                return {"ok": True, "cgroup_metrics": self.controller.get_cgroup_metrics()}
            if action == "cleanup_metrics":
                policies = self.controller.dump_policies().get("cgroup_policies", {})
                metrics = self.controller.get_cgroup_metrics()
                removed = self._cleanup_orphan_metrics(policies, metrics)
                return {"ok": True, "removed": removed, "reaped_metrics": self.reaped_metrics}
            if action == "policies":
                return {"ok": True, **self.controller.dump_policies()}
            if action == "shutdown":
                threading.Thread(target=self.shutdown, daemon=True).start()
                return {"ok": True, "stopping": True}
        return {"ok": False, "error": f"unsupported action: {action}"}

    def service_actions(self) -> None:
        if time.monotonic() < self._next_reap:
            return
        self._next_reap = time.monotonic() + 5
        with self.controller_lock:
            all_policies = self.controller.dump_policies()
            metrics = self.controller.get_cgroup_metrics()
            policies = all_policies.get("task_policies", {})
            for raw_pid in list(policies):
                pid = int(raw_pid)
                stale = pid in self.task_starts and process_start_time(pid) != self.task_starts[pid]
                if (not _pid_exists(pid) or stale) and self.controller.remove_task_policy(pid):
                    self.task_starts.pop(pid, None)
                    self.reaped_policies += 1
            for cgroup_id, path in list(self.cgroup_paths.items()):
                try:
                    removed_or_reused = path.stat().st_ino != cgroup_id
                except FileNotFoundError:
                    removed_or_reused = True
                if removed_or_reused:
                    if self.controller.remove_cgroup_policy(cgroup_id):
                        self.cgroup_paths.pop(cgroup_id, None)
                        self.reaped_policies += 1
            self._cleanup_orphan_metrics(
                all_policies.get("cgroup_policies", {}), metrics
            )
            self._adapt_fairness(all_policies, metrics)

    def _cleanup_orphan_metrics(
        self, policies: dict[Any, dict[str, Any]], metrics: dict[int, dict[str, int]]
    ) -> int:
        policy_ids = {int(cgroup_id) for cgroup_id in policies}
        removed = 0
        for raw_cgroup_id, values in list(metrics.items()):
            cgroup_id = int(raw_cgroup_id)
            if cgroup_id in policy_ids:
                continue
            if all(int(value) == 0 for value in values.values()):
                try:
                    remove_metrics = getattr(
                        self.controller, "remove_cgroup_metrics", self.controller.remove_cgroup_policy
                    )
                    if remove_metrics(cgroup_id):
                        removed += 1
                except Exception:
                    continue
        self.reaped_metrics += removed
        return removed

    def _adapt_fairness(
        self, policies: dict[str, Any], metrics: dict[int, dict[str, int]]
    ) -> None:
        classes = {
            int(policy["class_id"])
            for scope in ("task_policies", "cgroup_policies")
            for policy in policies.get(scope, {}).values()
        }
        cpu_pressure = _cpu_pressure_avg10()
        shares = _runtime_shares(policies.get("cgroup_policies", {}), self._last_metrics, metrics)
        current = int(self.fairness["background_interval"])
        mode, background = choose_background_interval(
            current,
            shares["background_share"],
            int(shares["sample_runtime_ns"]),
            cpu_pressure,
            bool(classes & {1, 2}) and 3 in classes,
            self.target_background_share["low"],
            self.target_background_share["high"],
        )
        default = SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL
        if (
            background != self.fairness["background_interval"]
            or default != self.fairness["default_interval"]
        ):
            self.controller.set_fairness(background, default)
        self.fairness = {
            "mode": mode,
            "background_interval": background,
            "default_interval": default,
            "cpu_pressure_avg10": cpu_pressure,
            "background_runtime_share": shares["background_share"],
        }
        self.control_telemetry = {
            **shares,
            "previous_background_interval": current,
            "selected_background_interval": background,
            "reason": mode,
            "target_low": self.target_background_share["low"],
            "target_high": self.target_background_share["high"],
        }
        self._last_metrics = metrics


if hasattr(socketserver, "UnixStreamServer"):
    class _ScxDaemonServer(  # type: ignore[misc]
        _ScxDaemonDispatch, socketserver.ThreadingMixIn, socketserver.UnixStreamServer
    ):
        pass
else:
    class _ScxDaemonServer:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("the scx daemon requires Linux Unix sockets")


class _ScxRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            self.request.settimeout(6.0)
            raw = self.rfile.readline(65537)
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise ValueError("daemon request must be one bounded JSON line")
            request = json.loads(raw.decode())
            response = self.server.dispatch(request)  # type: ignore[attr-defined]
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write(json.dumps(response).encode() + b"\n")


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _cpu_pressure_avg10(path: Path = Path("/proc/pressure/cpu")) -> float:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("some "):
                for field in line.split():
                    if field.startswith("avg10="):
                        return float(field.split("=", 1)[1])
    except (FileNotFoundError, ValueError):
        pass
    return 0.0


def _runtime_shares(
    policies: dict[Any, dict[str, Any]],
    previous: dict[int, dict[str, int]],
    current: dict[int, dict[str, int]],
) -> dict[str, float | int]:
    runtime_by_class: dict[int, int] = {}
    for raw_cgroup_id, metric in current.items():
        cgroup_id = int(raw_cgroup_id)
        policy = policies.get(cgroup_id) or policies.get(str(cgroup_id))
        if not policy:
            continue
        before = previous.get(cgroup_id, {}).get("runtime_ns", metric.get("runtime_ns", 0))
        delta = max(0, metric.get("runtime_ns", 0) - before)
        class_id = int(policy["class_id"])
        runtime_by_class[class_id] = runtime_by_class.get(class_id, 0) + delta
    total = sum(runtime_by_class.values())
    background = runtime_by_class.get(3, 0)
    return {
        "sample_runtime_ns": total,
        "background_runtime_ns": background,
        "background_share": background / total if total else 0.0,
    }


def choose_background_interval(
    current: int,
    background_share: float,
    sample_runtime_ns: int,
    cpu_pressure: float,
    mixed: bool,
    target_low: float = 0.12,
    target_high: float = 0.25,
) -> tuple[str, int]:
    """Adjust one step toward a 12%-25% background runtime target."""
    if not mixed:
        return "uncontended", SCX_FAIRNESS_BACKGROUND_DEFAULT
    if not sample_runtime_ns:
        return "awaiting_runtime_sample", current
    if background_share < target_low:
        return "runtime_share_low", max(SCX_FAIRNESS_BACKGROUND_MIN, current // 2)
    if background_share > target_high:
        return "runtime_share_high", min(SCX_FAIRNESS_BACKGROUND_MAX, current * 2)
    return "runtime_share_target", current


def serve_scx_daemon(
    socket_path: Path = DEFAULT_SOCKET, controller: ScxController | None = None
) -> None:
    """Own one sched_ext instance and expose concurrent policy updates."""
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        if ScxDaemonClient(socket_path).is_available():
            raise RuntimeError(f"scx daemon is already running at {socket_path}")
        socket_path.unlink()

    ctl = controller or ScxController(dry_run=False)
    started = ctl.start_scheduler()
    if not started:
        raise RuntimeError("sched_ext is unavailable")
    server = None
    try:
        server = _ScxDaemonServer(socket_path, ctl)
        os.chmod(socket_path, 0o660)
        server.serve_forever()
    finally:
        if server is not None:
            server.server_close()
        ctl.stop_scheduler()
        socket_path.unlink(missing_ok=True)
