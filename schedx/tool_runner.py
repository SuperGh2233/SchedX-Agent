from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
import signal
import select
import threading
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from schedx.controllers.scx_controller import (
    SCX_CLASS_BACKGROUND,
    SCX_CLASS_BATCH,
    SCX_CLASS_LATENCY,
    ScxController,
)
from schedx.scx_daemon import ScxDaemonClient


@dataclass(frozen=True)
class ToolProfile:
    intent: str
    scx_class: int
    scx_weight: int
    cpu_weight: int
    cpu_max: str
    memory_high: str
    memory_max: str
    pids_max: str


PROFILES = {
    "interactive": ToolProfile("interactive", SCX_CLASS_LATENCY, 10000, 1000, "max", "1G", "2G", "256"),
    "test": ToolProfile("test", SCX_CLASS_LATENCY, 5000, 500, "max", "2G", "3G", "1024"),
    "compile": ToolProfile("compile", SCX_CLASS_BATCH, 1500, 300, "max", "3G", "4G", "2048"),
    "package": ToolProfile("package", SCX_CLASS_BATCH, 1000, 200, "200000 100000", "2G", "3G", "1024"),
    "background": ToolProfile("background", SCX_CLASS_BACKGROUND, 100, 50, "100000 100000", "1G", "2G", "512"),
}


def infer_intent(command: Sequence[str]) -> str:
    text = " ".join(command).lower()
    if any(token in text for token in ("pytest", "unittest", "ctest", "cargo test", "go test")):
        return "test"
    if any(token in text for token in ("make", "ninja", "cmake --build", "cargo build", "gcc", "clang")):
        return "compile"
    if any(token in text for token in ("pip install", "dnf ", "apt ", "npm install", "cargo install")):
        return "package"
    if any(token in text for token in ("stress-ng", "benchmark", "train", "batch")):
        return "background"
    return "interactive"


def parse_resource_hint(value: str) -> tuple[str | None, dict[str, str | int]]:
    """Parse a portable Agent-to-OS resource-intent hint."""
    intent = None
    overrides: dict[str, str | int] = {}
    for item in value.split(","):
        key, _, raw = item.strip().partition(":")
        if not raw:
            continue
        if key == "intent" and raw in PROFILES:
            intent = raw
        elif key == "memory" and raw == "high":
            overrides.update(memory_high="3G", memory_max="4G")
        elif key == "memory" and raw == "low":
            overrides.update(memory_high="512M", memory_max="1G")
        elif key == "cpu" and raw == "high":
            overrides.update(cpu_weight=1000, cpu_max="max")
        elif key == "cpu" and raw == "low":
            overrides.update(cpu_weight=50, cpu_max="100000 100000")
    return intent, overrides


def recommend_next_hint(intent: str, metrics: dict, returncode: int) -> str:
    """Translate kernel pressure signals into a hint for the Agent's next run."""
    dimensions = [f"intent:{intent}"]
    memory_events = metrics.get("memory_events") or {}
    cpu_stat = metrics.get("cpu_stat") or {}
    if (
        memory_events.get("high", 0)
        or memory_events.get("oom", 0)
        or memory_events.get("oom_kill", 0)
    ):
        dimensions.append("memory:high")
    if cpu_stat.get("nr_throttled", 0):
        dimensions.append("cpu:high")
    return ",".join(dimensions) if len(dimensions) > 1 or returncode else ""


class ToolCallRunner:
    """Run one Agent tool call in an ephemeral hierarchical cgroup."""

    def __init__(
        self,
        root: Path = Path("/sys/fs/cgroup"),
        state_dir: Path = Path(".schedx/tool-runs"),
        native_scx: bool = True,
    ) -> None:
        self.root = root
        self.state_dir = state_dir
        self.native_scx = native_scx

    def run(
        self, command: Sequence[str], *, agent_id: str = "default", intent: str = "auto",
        profile_overrides: dict[str, str | int] | None = None, resource_hint: str = "",
        timeout: float = 300.0, output_limit: int = 1024 * 1024,
    ) -> dict:
        if not command:
            raise ValueError("tool command is required")
        if not math.isfinite(timeout) or timeout <= 0 or output_limit < 1:
            raise ValueError("tool timeout and output limit must be positive")
        hinted_intent, hinted_overrides = parse_resource_hint(resource_hint)
        selected_intent = hinted_intent or (infer_intent(command) if intent == "auto" else intent)
        if selected_intent not in PROFILES:
            raise ValueError(f"unsupported tool intent: {selected_intent}")
        profile = asdict(PROFILES[selected_intent])
        profile.update(hinted_overrides)
        profile.update(profile_overrides or {})
        run_id = f"tool-{uuid.uuid4().hex}"
        agent = self._safe_name(agent_id)
        parent = self.root / "schedx-agents" / agent
        tool = parent / run_id
        self.state_dir.mkdir(parents=True, exist_ok=True)
        scx, daemon = ScxController(dry_run=False), ScxDaemonClient()
        process = None
        cgroup_id = None
        started = time.monotonic()
        scx_started = scx_used = timed_out = False
        scx_error = ""
        descriptors = []
        readers, captures = [], {}
        result = None
        try:
            self._prepare_group(parent, tool, profile)
            cgroup_id = tool.stat().st_ino
            ready_r, ready_w = os.pipe()
            gate_r, gate_w = os.pipe()
            descriptors.extend((ready_r, ready_w, gate_r, gate_w))
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("tool_child.py")), str(ready_w), str(gate_r), str(tool), *command],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                pass_fds=(ready_w, gate_r), start_new_session=True,
            )
            for descriptor in (ready_w, gate_r):
                os.close(descriptor)
                descriptors.remove(descriptor)
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                capture = {"path": str(self.state_dir / f"{run_id}.{name}.log"), "bytes": 0, "error": ""}
                captures[name] = capture
                reader = threading.Thread(target=self._capture_output, args=(stream, capture, output_limit), daemon=True)
                reader.start()
                readers.append(reader)
            if not select.select([ready_r], [], [], min(timeout, 5.0))[0] or os.read(ready_r, 1) != b"R":
                raise RuntimeError("tool failed to enter its cgroup before startup deadline")
            if self.native_scx and scx.is_available():
                try:
                    if daemon.is_available():
                        scx_used = daemon.set_cgroup_policy(cgroup_id, int(profile["scx_class"]), int(profile["scx_weight"]), str(tool))
                    else:
                        scx_started = scx.start_scheduler()
                        scx_used = scx_started and scx.set_cgroup_policy(cgroup_id, int(profile["scx_class"]), int(profile["scx_weight"]))
                    if scx_used and scx_started:
                        scx.enable_adaptive_fairness()
                    if not scx_used:
                        scx_error = "scheduler did not acknowledge tool policy; using cgroup-only"
                except Exception as exc:
                    scx_error = str(exc)
                    scx.stop_scheduler()
                    scx_started = False
            os.write(gate_w, b"G")
            os.close(gate_w)
            descriptors.remove(gate_w)
            try:
                process.wait(timeout=max(0.01, timeout - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                timed_out = True
                self._stop_tree(process, tool)
            metrics = self._metrics(tool)
            scx_metrics = {}
            if scx_used and not scx_started:
                try:
                    scx_metrics = daemon.cgroup_metrics().get(str(cgroup_id), {})
                except (OSError, RuntimeError):
                    pass
            result = {
                "run_id": run_id, "agent_id": agent, "intent": selected_intent,
                "resource_hint": resource_hint, "command": list(command), "profile": profile,
                "cgroup": str(tool), "cgroup_id": cgroup_id, "pid": process.pid,
                "native_scx": scx_used, "scx_mode": ("standalone" if scx_started else "daemon") if scx_used else "cgroup",
                "scx_policy_scope": "cgroup" if scx_used else "none", "scx_error": scx_error,
                "cpu_control_support": scx.cpu_control_support(),
                "returncode": 124 if timed_out else process.returncode, "timed_out": timed_out,
                "duration_seconds": time.monotonic() - started, "metrics": metrics, "scx_cgroup_metrics": scx_metrics,
            }
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
            if process:
                self._stop_tree(process, tool)
            for reader in readers:
                reader.join(timeout=5)
            policy_cleanup = True
            if scx_used:
                try:
                    policy_cleanup = (scx if scx_started else daemon).remove_cgroup_policy(cgroup_id)
                except (OSError, RuntimeError):
                    policy_cleanup = False
            if scx_started:
                scx.stop_scheduler()
            cleanup = self._cleanup(tool, parent)
            if result is not None:
                result["cleanup"] = {"policy_removed": policy_cleanup, **cleanup}
                for name, capture in captures.items():
                    path = Path(capture["path"])
                    result[name] = path.read_bytes().decode("utf-8", errors="replace") if path.exists() else ""
                    result[name + "_path"] = str(path)
                    result[name + "_truncated"] = capture["bytes"] > output_limit
                    if capture["error"]:
                        result.setdefault("capture_errors", []).append(capture["error"])
        if result is None:
            raise RuntimeError("tool did not produce an execution result")
        if not result["cleanup"]["policy_removed"] or not result["cleanup"]["cgroup_removed"]:
            if result["returncode"] == 0:
                result["returncode"] = 125
        result["feedback"] = self._feedback(result["metrics"], result["returncode"])
        if result["cpu_control_support"]["cpu_max"] == "not_enforced" and not str(profile["cpu_max"]).startswith("max"):
            result["feedback"].append("the active native scheduler does not enforce CPU quotas; use the default scheduler for a hard quota")
        if timed_out:
            result["feedback"].append("tool exceeded its time limit; process tree stopped")
        result["next_resource_hint"] = recommend_next_hint(selected_intent, result["metrics"], result["returncode"])
        result["retry_recommended"] = bool(result["next_resource_hint"])
        self._save(result)
        return result

    @staticmethod
    def _capture_output(stream, capture: dict, limit: int) -> None:
        try:
            with Path(capture["path"]).open("wb") as output:
                while chunk := stream.read(65536):
                    remaining = max(0, limit - capture["bytes"])
                    capture["bytes"] += len(chunk)
                    if remaining:
                        try:
                            output.write(chunk[:remaining])
                        except OSError as exc:
                            capture["error"] = str(exc)
        except OSError as exc:
            capture["error"] = str(exc)
            while chunk := stream.read(65536):
                capture["bytes"] += len(chunk)
        finally:
            stream.close()

    @staticmethod
    def _stop_tree(process, tool: Path) -> None:
        kill = tool / "cgroup.kill"
        if kill.exists():
            try:
                kill.write_text("1", encoding="utf-8")
            except OSError:
                pass
        procs = tool / "cgroup.procs"
        if process.poll() is None or (procs.exists() and procs.read_text().strip()):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.wait(timeout=5)

    def _prepare_group(self, parent: Path, tool: Path, profile: dict) -> None:
        if not (self.root / "cgroup.controllers").exists():
            raise RuntimeError("cgroup v2 is required for tool-run")
        self._enable(self.root, ("cpu", "memory", "pids"))
        parent.parent.mkdir(exist_ok=True)
        self._enable(parent.parent, ("cpu", "memory", "pids"))
        parent.mkdir(exist_ok=True)
        self._enable(parent, ("cpu", "memory", "pids"))
        tool.mkdir(exist_ok=False)
        self._write_if_exists(tool / "cpu.weight", str(profile["cpu_weight"]))
        self._write_if_exists(tool / "cpu.max", str(profile["cpu_max"]))
        self._write_if_exists(tool / "memory.high", str(profile["memory_high"]))
        self._write_if_exists(tool / "memory.max", str(profile["memory_max"]))
        self._write_if_exists(tool / "pids.max", str(profile["pids_max"]))

    @staticmethod
    def _move_self(tool: Path):
        def move() -> None:
            (tool / "cgroup.procs").write_text(str(os.getpid()), encoding="utf-8")

        return move

    def _enable(self, path: Path, controllers: Sequence[str]) -> None:
        subtree = path / "cgroup.subtree_control"
        if not subtree.exists():
            return
        available = set((path / "cgroup.controllers").read_text(encoding="utf-8").split())
        for controller in controllers:
            if controller in available:
                try:
                    subtree.write_text(f"+{controller}", encoding="utf-8")
                except OSError as exc:
                    raise RuntimeError(f"cannot enable {controller} controller at {path}: {exc}") from exc

    @staticmethod
    def _write_if_exists(path: Path, value: str) -> None:
        if not path.exists():
            raise RuntimeError(f"required resource control is unavailable: {path}")
        path.write_text(value, encoding="utf-8")

    def _metrics(self, tool: Path) -> dict:
        return {
            "memory_current_bytes": self._read_int(tool / "memory.current"),
            "memory_peak_bytes": self._read_int(tool / "memory.peak"),
            "memory_events": self._read_key_values(tool / "memory.events"),
            "cpu_stat": self._read_key_values(tool / "cpu.stat"),
            "pids_peak": self._read_int(tool / "pids.peak"),
        }

    @staticmethod
    def _feedback(metrics: dict, returncode: int) -> list[str]:
        feedback = []
        memory_events = metrics.get("memory_events") or {}
        cpu_stat = metrics.get("cpu_stat") or {}
        if memory_events.get("high", 0):
            feedback.append("memory pressure detected; retry with smaller parallelism or a streaming tool")
        if memory_events.get("oom", 0) or memory_events.get("oom_kill", 0):
            feedback.append("memory limit reached; split the operation before retrying")
        if cpu_stat.get("nr_throttled", 0):
            feedback.append("CPU throttling detected; reduce parallel workers or request a compile profile")
        if returncode:
            feedback.append("tool failed; use collected pressure metrics when planning the retry")
        return feedback

    def _save(self, result: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / f"{result['run_id']}.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _cleanup(tool: Path, parent: Path) -> dict:
        errors = []
        for path in (tool, parent, parent.parent):
            try:
                path.rmdir()
            except FileNotFoundError:
                continue
            except OSError as exc:
                if path == tool:
                    errors.append(str(exc))
        return {"cgroup_removed": not tool.exists(), "errors": errors}

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", value)[:64].strip(".") or "default"

    @staticmethod
    def _read_int(path: Path) -> int | None:
        try:
            value = path.read_text(encoding="utf-8").strip()
            return None if value == "max" else int(value)
        except (FileNotFoundError, ValueError):
            return None

    @staticmethod
    def _read_key_values(path: Path) -> dict[str, int]:
        try:
            return {
                parts[0]: int(parts[1])
                for line in path.read_text(encoding="utf-8").splitlines()
                if len(parts := line.split()) == 2
            }
        except FileNotFoundError:
            return {}


def emit_tool_result(result: dict) -> None:
    if result["stdout"]:
        sys.stdout.write(result["stdout"])
    if result["stderr"]:
        sys.stderr.write(result["stderr"])
    for message in result["feedback"]:
        sys.stderr.write(f"[schedx-feedback] {message}\n")
    if result["next_resource_hint"]:
        sys.stderr.write(f"[schedx-next-hint] {result['next_resource_hint']}\n")
    sys.stderr.write(
        "[schedx-metrics] "
        + json.dumps(
            {
                "intent": result["intent"],
                "duration_seconds": round(result["duration_seconds"], 4),
                "memory_peak_bytes": result["metrics"]["memory_peak_bytes"],
                "native_scx": result["native_scx"],
                "scx_mode": result["scx_mode"],
                "cpu_control_support": result["cpu_control_support"],
            }
        )
        + "\n"
    )
