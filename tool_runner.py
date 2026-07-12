from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from schedx.controllers.scx_controller import (
    SCX_CLASS_BACKGROUND,
    SCX_CLASS_BATCH,
    SCX_CLASS_LATENCY,
    ScxController,
)


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
        self,
        command: Sequence[str],
        *,
        agent_id: str = "default",
        intent: str = "auto",
        profile_overrides: dict[str, str | int] | None = None,
    ) -> dict:
        if not command:
            raise ValueError("tool command is required")
        selected_intent = infer_intent(command) if intent == "auto" else intent
        if selected_intent not in PROFILES:
            raise ValueError(f"unsupported tool intent: {selected_intent}")
        profile = asdict(PROFILES[selected_intent])
        profile.update(profile_overrides or {})

        run_id = f"tool-{int(time.time() * 1000)}-{os.getpid()}"
        agent = self._safe_name(agent_id)
        parent = self.root / "schedx-agents" / agent
        tool = parent / run_id
        self._prepare_group(parent, tool, profile)

        scx = ScxController(dry_run=False)
        scx_started = False
        scx_error = ""
        started = time.monotonic()
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                list(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=self._move_self(tool),
            )
            if self.native_scx and scx.is_available():
                try:
                    scx_started = scx.start_scheduler()
                    if scx_started:
                        scx.set_task_policy(
                            process.pid, int(profile["scx_class"]), int(profile["scx_weight"])
                        )
                except Exception as exc:
                    # cgroup controls remain valid when another native scx
                    # scheduler already owns the struct_ops link.
                    scx_error = str(exc)
            stdout, stderr = process.communicate()
            elapsed = time.monotonic() - started
            metrics = self._metrics(tool)
            feedback = self._feedback(metrics, process.returncode)
            result = {
                "run_id": run_id,
                "agent_id": agent,
                "intent": selected_intent,
                "command": list(command),
                "profile": profile,
                "cgroup": str(tool),
                "native_scx": scx_started,
                "scx_error": scx_error,
                "pid": process.pid,
                "returncode": process.returncode,
                "duration_seconds": elapsed,
                "metrics": metrics,
                "feedback": feedback,
                "stdout": stdout,
                "stderr": stderr,
            }
            self._save(result)
            return result
        finally:
            if scx_started:
                scx.stop_scheduler()
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            self._cleanup(tool, parent)

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
                except OSError:
                    pass

    @staticmethod
    def _write_if_exists(path: Path, value: str) -> None:
        if path.exists():
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
    def _cleanup(tool: Path, parent: Path) -> None:
        try:
            tool.rmdir()
            parent.rmdir()
            parent.parent.rmdir()
        except OSError:
            pass

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", value)[:64] or "default"

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
    sys.stderr.write(
        "[schedx-metrics] "
        + json.dumps(
            {
                "intent": result["intent"],
                "duration_seconds": round(result["duration_seconds"], 4),
                "memory_peak_bytes": result["metrics"]["memory_peak_bytes"],
                "native_scx": result["native_scx"],
            }
        )
        + "\n"
    )
