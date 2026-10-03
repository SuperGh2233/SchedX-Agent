from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import queue
import threading
from collections import deque
from functools import wraps
from dataclasses import dataclass
from pathlib import Path
from typing import Any

def _serialized(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self._command_lock:
            return method(self, *args, **kwargs)
    return invoke


# Workload classification constants - must match BPF code
SCX_CLASS_UNKNOWN = 0
SCX_CLASS_LATENCY = 1
SCX_CLASS_BATCH = 2
SCX_CLASS_BACKGROUND = 3

# Class name mapping
SCX_CLASS_NAMES = {
    SCX_CLASS_UNKNOWN: "unknown",
    SCX_CLASS_LATENCY: "latency",
    SCX_CLASS_BATCH: "batch",
    SCX_CLASS_BACKGROUND: "background",
}

# Default weights
SCX_WEIGHT_DEFAULTS = {
    SCX_CLASS_UNKNOWN: 1000,
    SCX_CLASS_LATENCY: 10000,
    SCX_CLASS_BATCH: 1000,
    SCX_CLASS_BACKGROUND: 100,
}

# A service interval of 64 preserved at least 25% of baseline background
# progress in the formal SP4 benchmark. Keep runtime and benchmark defaults in
# one place so normal Agent execution is tested with the same safety setting.
SCX_FAIRNESS_BACKGROUND_DEFAULT = 64
SCX_FAIRNESS_BACKGROUND_MIN = 1
SCX_FAIRNESS_BACKGROUND_MAX = 4096
SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL = 32
SCX_FAIRNESS_THROUGHPUT_BACKGROUND = 40


@dataclass
class ScxStats:
    """Dispatch statistics from the BPF scheduler."""
    latency_dispatches: int = 0
    batch_dispatches: int = 0
    background_dispatches: int = 0
    default_dispatches: int = 0

    @property
    def total(self) -> int:
        return (self.latency_dispatches + self.batch_dispatches +
                self.background_dispatches + self.default_dispatches)

    def to_dict(self) -> dict[str, int | str]:
        return {
            "latency_dispatches": self.latency_dispatches,
            "batch_dispatches": self.batch_dispatches,
            "background_dispatches": self.background_dispatches,
            "default_dispatches": self.default_dispatches,
            "total": self.total,
            "counter_source": "enqueue",
        }


class ScxController:
    """Controller for sched_ext BPF scheduler integration.

    This controller manages the scx_agent BPF scheduler lifecycle and
    provides an interface for updating task/cgroup scheduling policies.
    """

    ALLOWLIST = {"scx_simple", "scx_rusty", "scx_agent"}

    def __init__(
        self,
        sys_root: Path = Path("/sys/kernel/sched_ext"),
        dry_run: bool = True,
        scheduler_binary: str = "scx_agent",
        background_interval: int | None = None,
        command_timeout: float = 5.0,
    ) -> None:
        self.sys_root = sys_root
        self.dry_run = dry_run
        self.scheduler_binary = scheduler_binary
        if background_interval is None:
            background_interval = int(
                os.environ.get(
                    "SCHEDX_SCX_BACKGROUND_INTERVAL",
                    str(SCX_FAIRNESS_BACKGROUND_DEFAULT),
                )
            )
        if not SCX_FAIRNESS_BACKGROUND_MIN <= background_interval <= SCX_FAIRNESS_BACKGROUND_MAX:
            raise ValueError(
                "background_interval must be between "
                f"{SCX_FAIRNESS_BACKGROUND_MIN} and {SCX_FAIRNESS_BACKGROUND_MAX}"
            )
        self.background_interval = background_interval
        self._process: subprocess.Popen | None = None
        self._pipe_path: Path | None = None
        self.command_timeout = command_timeout
        self._command_lock = threading.RLock()
        self._stdout_lines = queue.Queue()
        self._stderr_tail = deque(maxlen=100)
        self._adaptive_stop = threading.Event()
        self._adaptive_thread = None
        self._adaptive_report = {}

    def is_available(self) -> bool:
        """Check if sched_ext is available on this system."""
        return self.sys_root.exists()

    def state(self) -> str:
        """Get current sched_ext state."""
        path = self.sys_root / "state"
        if not path.exists():
            return "unavailable"
        return path.read_text(encoding="utf-8", errors="replace").strip()

    def current_scheduler(self) -> str:
        """Get currently loaded scheduler name."""
        for name in ("root/ops", "ops", "scheduler"):
            path = self.sys_root / name
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace").strip()
        return "none" if self.is_available() else "unavailable"

    def status(self) -> dict[str, Any]:
        """Get comprehensive scheduler status."""
        return {
            "available": self.is_available(),
            "state": self.state(),
            "current_scheduler": self.current_scheduler(),
            "allowlist": ", ".join(sorted(self.ALLOWLIST)),
            "process_running": self._process is not None and self._process.poll() is None,
            "background_interval": self.background_interval,
            "cpu_control_support": self.cpu_control_support(),
        }

    def cpu_control_support(self) -> dict[str, str]:
        if self.state() != "enabled":
            available = Path("/sys/fs/cgroup/cgroup.controllers").exists()
            return {"cpu_weight": "cgroup_v2" if available else "unavailable", "cpu_max": "cgroup_v2" if available else "unavailable"}
        if self.current_scheduler() == "schedx_agent":
            quota = "not_enforced" if os.uname().release == "6.6.0-159.4.3.154.oe2403sp4.schedx1" else "kernel_dependent"
            return {"cpu_weight": "schedx_policy_map", "cpu_max": quota}
        return {"cpu_weight": "scheduler_dependent", "cpu_max": "scheduler_dependent"}

    @_serialized
    def start_scheduler(
        self,
        scheduler_name: str = "scx_agent",
        args: list[str] | None = None,
    ) -> bool:
        """Start the BPF scheduler.

        Args:
            scheduler_name: Name of the scheduler binary
            args: Additional arguments to pass to the scheduler

        Returns:
            True if scheduler started successfully
        """
        if scheduler_name not in self.ALLOWLIST:
            raise ValueError(f"{scheduler_name} is not in the scx scheduler allowlist")

        if not self.is_available():
            return False

        binary = shutil.which(scheduler_name)
        if not binary:
            raise FileNotFoundError(f"{scheduler_name} was not found in PATH")

        command = [binary] + list(args or [])

        if self.dry_run:
            return True

        # Start the scheduler process with pipe for communication
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        self._stdout_lines = queue.Queue()
        self._stderr_tail.clear()
        threading.Thread(target=self._pump_output, args=(self._process.stdout, self._stdout_lines), daemon=True).start()
        threading.Thread(target=self._pump_output, args=(self._process.stderr, None), daemon=True).start()

        # Wait a moment for initialization
        time.sleep(0.1)

        # Check if process started successfully
        if self._process.poll() is not None:
            stderr = "".join(self._stderr_tail)
            raise RuntimeError(f"Failed to start {scheduler_name}: {stderr}")

        # Consume the initial line-delimited prompt so the next command reads
        # its own response rather than returning immediately.
        self._read_until_prompt()
        # Avoid strict class-priority starvation even outside daemon mode.
        if not self.set_fairness(
            background_interval=self.background_interval,
            default_interval=SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL,
        ):
            self.stop_scheduler()
            raise RuntimeError("scheduler did not acknowledge service guarantees")
        return True

    @_serialized
    def stop_scheduler(self) -> bool:
        """Stop the BPF scheduler.

        Returns:
            True if scheduler stopped successfully
        """
        self._adaptive_stop.set()
        if not self._process:
            return True

        if self.dry_run:
            self._process = None
            return True

        try:
            # Send quit command
            self._send_command("quit")

            # Wait for graceful shutdown
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Force kill if graceful shutdown fails
                self._process.send_signal(signal.SIGTERM)
                self._process.wait(timeout=2)

        except Exception:
            # Force kill on any error
            try:
                self._process.kill()
                self._process.wait(timeout=1)
            except Exception:
                pass

        self._process = None
        return True

    @_serialized
    def set_task_policy(
        self,
        pid: int,
        class_id: int,
        weight: int | None = None,
    ) -> bool:
        """Set scheduling policy for a specific task.

        Args:
            pid: Process ID
            class_id: Workload class (SCX_CLASS_*)
            weight: Optional weight (1-10000), uses class default if None

        Returns:
            True if policy was set successfully
        """
        if class_id not in SCX_CLASS_NAMES:
            raise ValueError(f"Invalid class_id: {class_id}")

        if weight is None:
            weight = SCX_WEIGHT_DEFAULTS.get(class_id, 1000)

        if not 1 <= weight <= 10000:
            raise ValueError("weight must be between 1 and 10000")

        if self.dry_run:
            return True

        if not self._process or self._process.poll() is not None:
            raise RuntimeError("Scheduler is not running")

        cmd = f"set task {pid} {class_id} {weight}"
        if not self._send_command(cmd):
            return False
        return "policy updated" in self._read_until_prompt()

    @_serialized
    def set_cgroup_policy(
        self,
        cgroup_id: int,
        class_id: int,
        weight: int | None = None,
    ) -> bool:
        """Set scheduling policy for a cgroup.

        Args:
            cgroup_id: Cgroup ID
            class_id: Workload class (SCX_CLASS_*)
            weight: Optional weight (1-10000), uses class default if None

        Returns:
            True if policy was set successfully
        """
        if class_id not in SCX_CLASS_NAMES:
            raise ValueError(f"Invalid class_id: {class_id}")

        if weight is None:
            weight = SCX_WEIGHT_DEFAULTS.get(class_id, 1000)

        if not 1 <= weight <= 10000:
            raise ValueError("weight must be between 1 and 10000")

        if self.dry_run:
            return True

        if not self._process or self._process.poll() is not None:
            raise RuntimeError("Scheduler is not running")

        cmd = f"set cgroup {cgroup_id} {class_id} {weight}"
        if not self._send_command(cmd):
            return False
        return "policy updated" in self._read_until_prompt()

    @_serialized
    def remove_task_policy(self, pid: int) -> bool:
        """Remove scheduling policy for a specific task.

        Args:
            pid: Process ID

        Returns:
            True if policy was removed successfully
        """
        if self.dry_run:
            return True

        if not self._process or self._process.poll() is not None:
            raise RuntimeError("Scheduler is not running")

        cmd = f"remove task {pid}"
        if not self._send_command(cmd):
            return False
        return "policy removed" in self._read_until_prompt()

    @_serialized
    def remove_cgroup_policy(self, cgroup_id: int) -> bool:
        """Remove scheduling policy for a cgroup.

        Args:
            cgroup_id: Cgroup ID

        Returns:
            True if policy was removed successfully
        """
        if self.dry_run:
            return True

        if not self._process or self._process.poll() is not None:
            raise RuntimeError("Scheduler is not running")

        cmd = f"remove cgroup {cgroup_id}"
        if not self._send_command(cmd):
            return False
        return "policy removed" in self._read_until_prompt()

    @_serialized
    def remove_cgroup_metrics(self, cgroup_id: int) -> bool:
        """Remove scheduler metrics for a cgroup without requiring a policy."""
        if self.dry_run:
            return True

        if not self._process or self._process.poll() is not None:
            raise RuntimeError("Scheduler is not running")

        cmd = f"remove cgroup-metrics {cgroup_id}"
        if not self._send_command(cmd):
            return False
        return "metrics removed" in self._read_until_prompt()

    @_serialized
    def set_fairness(self, background_interval: int, default_interval: int) -> bool:
        """Set service intervals while retaining a floor for ordinary tasks."""
        if not 0 <= background_interval <= 100000:
            raise ValueError("background_interval must be between 0 and 100000")
        if not 1 <= default_interval <= 100000:
            raise ValueError("default_interval must be between 1 and 100000")
        if self.dry_run:
            return True
        if not self._process or self._process.poll() is not None:
            raise RuntimeError("Scheduler is not running")
        if not self._send_command(f"set fairness {background_interval} {default_interval}"):
            return False
        success = "fairness updated" in self._read_until_prompt()
        if success:
            self.background_interval = background_interval
        return success

    def enable_adaptive_fairness(self) -> None:
        if self.dry_run or (self._adaptive_thread and self._adaptive_thread.is_alive() and not self._adaptive_stop.is_set()):
            return
        self._adaptive_stop = threading.Event()
        self._adaptive_thread = threading.Thread(target=self._adapt_runtime, args=(self._adaptive_stop,), daemon=True)
        self._adaptive_thread.start()

    def _adapt_runtime(self, stop: threading.Event) -> None:
        from schedx.scx_daemon import choose_background_interval
        previous = None
        while not stop.wait(1.0):
            try:
                current = self.get_class_metrics()
                if previous is not None:
                    delta = {key: max(0, row["runtime_ns"] - previous.get(key, {}).get("runtime_ns", row["runtime_ns"])) for key, row in current.items()}
                    foreground = delta.get(1, 0) + delta.get(2, 0)
                    background = delta.get(3, 0)
                    if foreground and background:
                        share = background / (foreground + background)
                        reason, interval = choose_background_interval(self.background_interval, share, foreground + background, 0, True, 0.15, 0.25)
                        if stop.is_set():
                            break
                        if interval != self.background_interval:
                            self.set_fairness(interval, SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL)
                        self._adaptive_report = {"reason": reason, "background_runtime_share": share, "interval": self.background_interval}
                previous = current
            except (OSError, RuntimeError, ValueError):
                if not self._process or self._process.poll() is not None:
                    break

    @_serialized
    def get_stats(self) -> ScxStats:
        """Get dispatch statistics from the scheduler.

        Returns:
            ScxStats object with dispatch counts
        """
        if self.dry_run:
            return ScxStats()

        if not self._process or self._process.poll() is not None:
            return ScxStats()

        # Send stats command and parse output
        self._send_command("stats")

        # Read output until next prompt
        output = self._read_until_prompt()

        # Parse statistics from output
        stats = ScxStats()
        for line in output.split("\n"):
            if "Latency dispatches:" in line:
                stats.latency_dispatches = int(line.split(":")[1].strip())
            elif "Batch dispatches:" in line:
                stats.batch_dispatches = int(line.split(":")[1].strip())
            elif "Background dispatches:" in line:
                stats.background_dispatches = int(line.split(":")[1].strip())
            elif "Default dispatches:" in line:
                stats.default_dispatches = int(line.split(":")[1].strip())

        return stats

    @_serialized
    def get_cgroup_metrics(self) -> dict[int, dict[str, int]]:
        """Return cumulative scheduler metrics for managed cgroups."""
        if self.dry_run or not self._process or self._process.poll() is not None:
            return {}
        self._send_command("metrics")
        output = self._read_until_prompt()
        metrics: dict[int, dict[str, int]] = {}
        for line in output.splitlines():
            if not line.startswith("cgroup_id="):
                continue
            values = {
                key: int(value)
                for part in line.split()
                for key, value in [part.split("=", 1)]
            }
            cgroup_id = values.pop("cgroup_id")
            metrics[cgroup_id] = values
        return metrics

    @_serialized
    def get_class_metrics(self) -> dict[int, dict[str, int]]:
        if self.dry_run or not self._process or self._process.poll() is not None:
            return {}
        self._send_command("class_metrics")
        metrics = {}
        for line in self._read_until_prompt().splitlines():
            if line.startswith("class_id="):
                fields = dict(part.split("=", 1) for part in line.split())
                class_id = int(fields.pop("class_id"))
                metrics[class_id] = {key: int(value) for key, value in fields.items()}
        return metrics

    @_serialized
    def dump_policies(self) -> dict[str, Any]:
        """Dump all current policies.

        Returns:
            Dictionary with task and cgroup policies
        """
        if self.dry_run:
            return {"task_policies": {}, "cgroup_policies": {}}

        if not self._process or self._process.poll() is not None:
            return {"task_policies": {}, "cgroup_policies": {}}

        # Send dump command
        self._send_command("dump")

        # Read output
        output = self._read_until_prompt()

        # Parse policies
        task_policies = {}
        cgroup_policies = {}

        current_section = None
        for line in output.split("\n"):
            line = line.strip()
            if "Task Policies" in line:
                current_section = "task"
            elif "Cgroup Policies" in line:
                current_section = "cgroup"
            elif line.startswith("pid=") and current_section == "task":
                parts = line.split()
                pid = int(parts[0].split("=")[1])
                class_id = int(parts[1].split("=")[1])
                weight = int(parts[2].split("=")[1])
                task_policies[pid] = {"class_id": class_id, "weight": weight}
            elif line.startswith("cgroup_id=") and current_section == "cgroup":
                parts = line.split()
                cgroup_id = int(parts[0].split("=")[1])
                class_id = int(parts[1].split("=")[1])
                weight = int(parts[2].split("=")[1])
                cgroup_policies[cgroup_id] = {"class_id": class_id, "weight": weight}

        return {
            "task_policies": task_policies,
            "cgroup_policies": cgroup_policies,
        }

    def _send_command(self, cmd: str) -> bool:
        """Send a command to the scheduler process."""
        if not self._process or not self._process.stdin:
            return False

        try:
            self._process.stdin.write(cmd + "\n")
            self._process.stdin.flush()
            return True
        except Exception:
            return False

    def _pump_output(self, stream, output_queue) -> None:
        if stream is None:
            return
        try:
            for line in stream:
                if output_queue is not None:
                    output_queue.put(line)
                else:
                    self._stderr_tail.append(line[-4096:])
        finally:
            if output_queue is not None:
                output_queue.put(None)
            stream.close()

    def _read_until_prompt(self) -> str:
        if not self._process:
            return ""
        deadline = time.monotonic() + self.command_timeout
        output = []
        while True:
            remaining = deadline - time.monotonic()
            try:
                line = self._stdout_lines.get(timeout=max(0, remaining))
            except queue.Empty:
                process = self._process
                process.kill()
                process.wait(timeout=2)
                self._process = None
                raise TimeoutError("scheduler response deadline exceeded: " + "".join(self._stderr_tail)[-4096:])
            if line is None:
                raise RuntimeError("scheduler closed its response stream: " + "".join(self._stderr_tail)[-4096:])
            if "schedx>" in line:
                break
            output.append(line)
        return "".join(output)

    @staticmethod
    def classify_to_scx_class(workload_type: str) -> int:
        """Convert SchedX workload type to SCX class ID.

        Args:
            workload_type: Workload type string from classifier

        Returns:
            SCX class ID
        """
        mapping = {
            "latency_sensitive": SCX_CLASS_LATENCY,
            "batch_compute": SCX_CLASS_BATCH,
            "background_noise": SCX_CLASS_BACKGROUND,
            "unknown": SCX_CLASS_UNKNOWN,
        }
        return mapping.get(workload_type, SCX_CLASS_UNKNOWN)

    @staticmethod
    def scx_class_name(class_id: int) -> str:
        """Get human-readable name for SCX class ID."""
        return SCX_CLASS_NAMES.get(class_id, "unknown")
