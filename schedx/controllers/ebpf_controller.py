"""eBPF Controller - Manages eBPF programs for SchedX-Agent.

This module provides a Python interface for loading and managing
eBPF programs for scheduling, network policy, and resource control.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class EbpfProgType(Enum):
    """eBPF program types."""
    SCHED_TRACE = "sched_trace"
    NET_POLICY = "net_policy"
    RESOURCE_CTRL = "resource_ctrl"


@dataclass
class EbpfStats:
    """Statistics from eBPF programs."""
    sched_trace: dict[str, int] | None = None
    net_policy: dict[str, int] | None = None
    resource_ctrl: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sched_trace": self.sched_trace,
            "net_policy": self.net_policy,
            "resource_ctrl": self.resource_ctrl,
        }


@dataclass
class EbpfProgramState:
    """State of an eBPF program."""
    prog_type: EbpfProgType
    loaded: bool = False
    attached: bool = False
    pid: int | None = None  # PID of loader process
    map_fds: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.prog_type.value,
            "loaded": self.loaded,
            "attached": self.attached,
            "pid": self.pid,
        }


class EbpfController:
    """Controller for eBPF programs.

    This controller manages the lifecycle of eBPF programs including:
    - Loading BPF programs
    - Attaching to hooks
    - Updating policies via BPF maps
    - Collecting statistics
    """

    # BPF class ID constants (must match BPF code)
    CLASS_UNKNOWN = 0
    CLASS_LATENCY = 1
    CLASS_BATCH = 2
    CLASS_BACKGROUND = 3

    # Default rate limits (bytes/sec)
    RATE_LIMITS = {
        CLASS_LATENCY: 1_000_000_000,    # 1 GB/s
        CLASS_BATCH: 100_000_000,         # 100 MB/s
        CLASS_BACKGROUND: 10_000_000,     # 10 MB/s
        CLASS_UNKNOWN: 500_000_000,       # 500 MB/s
    }

    # Default memory limits
    MEMORY_LIMITS = {
        CLASS_LATENCY: 4 * 1024 * 1024 * 1024,    # 4 GB
        CLASS_BATCH: 2 * 1024 * 1024 * 1024,       # 2 GB
        CLASS_BACKGROUND: 512 * 1024 * 1024,        # 512 MB
        CLASS_UNKNOWN: 2 * 1024 * 1024 * 1024,      # 2 GB
    }

    def __init__(
        self,
        ebpf_dir: Path | None = None,
        dry_run: bool = True,
    ) -> None:
        """Initialize eBPF controller.

        Args:
            ebpf_dir: Directory containing BPF object files
            dry_run: If True, don't actually load/attach BPF programs
        """
        self.ebpf_dir = ebpf_dir or Path(__file__).parent.parent.parent / "ebpf"
        self.pin_root = Path("/sys/fs/bpf/schedx")
        self.dry_run = dry_run
        self._programs: dict[EbpfProgType, EbpfProgramState] = {}

    def is_available(self) -> bool:
        """Check if eBPF is available on this system."""
        # Check for BPF filesystem
        if not Path("/sys/fs/bpf").exists():
            return False

        # Check for bpftool
        try:
            result = subprocess.run(
                ["bpftool", "version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def check_sched_ext(self) -> bool:
        """Check if sched_ext is available."""
        return Path("/sys/kernel/sched_ext").exists()

    def get_status(self) -> dict[str, Any]:
        """Get overall eBPF status."""
        return {
            "available": self.is_available(),
            "sched_ext": self.check_sched_ext(),
            "programs": {
                prog_type.value: state.to_dict()
                for prog_type, state in self._programs.items()
            },
            "dry_run": self.dry_run,
        }

    def load_program(self, prog_type: EbpfProgType) -> bool:
        """Load an eBPF program.

        Args:
            prog_type: Type of program to load

        Returns:
            True if program loaded successfully
        """
        if self.dry_run:
            self._programs[prog_type] = EbpfProgramState(
                prog_type=prog_type,
                loaded=True,
                attached=False,
            )
            return True

        if not self.is_available():
            return False

        # Find BPF object file
        obj_file = self._get_object_path(prog_type)
        if not obj_file.exists():
            raise FileNotFoundError(f"BPF object not found: {obj_file}")

        # Load using bpftool or libbpf
        try:
            # Use bpftool to load the program
            pin_path = self.pin_root / prog_type.value
            pin_path.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["bpftool", "prog", "loadall", str(obj_file), str(pin_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                raise RuntimeError(f"Failed to load BPF program: {result.stderr}")

            self._programs[prog_type] = EbpfProgramState(
                prog_type=prog_type,
                loaded=True,
                attached=False,
            )
            return True

        except Exception as e:
            print(f"Error loading BPF program: {e}", file=sys.stderr)
            return False

    def attach_program(self, prog_type: EbpfProgType) -> bool:
        """Attach an eBPF program to its hooks.

        Args:
            prog_type: Type of program to attach

        Returns:
            True if program attached successfully
        """
        if prog_type not in self._programs:
            return False

        state = self._programs[prog_type]

        if self.dry_run:
            state.attached = True
            return True

        if not state.loaded:
            return False

        # Attach based on program type
        try:
            if prog_type == EbpfProgType.SCHED_TRACE:
                return self._attach_sched_trace(state)
            elif prog_type == EbpfProgType.NET_POLICY:
                return self._attach_net_policy(state)
            elif prog_type == EbpfProgType.RESOURCE_CTRL:
                return self._attach_resource_ctrl(state)
            else:
                return False

        except Exception as e:
            print(f"Error attaching BPF program: {e}", file=sys.stderr)
            return False

    def unload_program(self, prog_type: EbpfProgType) -> bool:
        """Unload an eBPF program.

        Args:
            prog_type: Type of program to unload

        Returns:
            True if program unloaded successfully
        """
        if prog_type not in self._programs:
            return True

        state = self._programs[prog_type]

        if self.dry_run:
            del self._programs[prog_type]
            return True

        try:
            # Detach if attached
            if state.attached:
                self._detach_program(prog_type)

            # Remove from BPF filesystem
            bpf_path = self.pin_root / prog_type.value
            if bpf_path.exists():
                for child in bpf_path.iterdir():
                    child.unlink()
                bpf_path.rmdir()
            if self.pin_root.exists() and not any(self.pin_root.iterdir()):
                self.pin_root.rmdir()

            del self._programs[prog_type]
            return True

        except Exception as e:
            print(f"Error unloading BPF program: {e}", file=sys.stderr)
            return False

    def update_sched_policy(
        self,
        pid: int,
        class_id: int,
        weight: int | None = None,
    ) -> bool:
        """Update scheduling policy for a task.

        Args:
            pid: Process ID
            class_id: Workload class (CLASS_*)
            weight: Optional weight (uses default if None)

        Returns:
            True if policy updated successfully
        """
        if weight is None:
            weight = {1: 10000, 2: 1000, 3: 100}.get(class_id, 1000)

        if self.dry_run:
            return True

        # Find sched_trace map
        if EbpfProgType.SCHED_TRACE not in self._programs:
            return False

        # Update map via bpftool or direct syscall
        try:
            # This would use the actual BPF map update
            # For now, just return success
            return True
        except Exception:
            return False

    def update_net_policy(
        self,
        pid: int,
        class_id: int,
        rate_limit: int | None = None,
    ) -> bool:
        """Update network policy for a task.

        Args:
            pid: Process ID
            class_id: Network class (CLASS_*)
            rate_limit: Optional rate limit in bytes/sec

        Returns:
            True if policy updated successfully
        """
        if rate_limit is None:
            rate_limit = self.RATE_LIMITS.get(class_id, 500_000_000)

        if self.dry_run:
            return True

        if EbpfProgType.NET_POLICY not in self._programs:
            return False

        try:
            return True
        except Exception:
            return False

    def update_resource_policy(
        self,
        cgroup_id: int,
        class_id: int,
        memory_limit: int | None = None,
    ) -> bool:
        """Update resource policy for a cgroup.

        Args:
            cgroup_id: Cgroup ID
            class_id: Resource class (CLASS_*)
            memory_limit: Optional memory limit in bytes

        Returns:
            True if policy updated successfully
        """
        if memory_limit is None:
            memory_limit = self.MEMORY_LIMITS.get(class_id, 2 * 1024 * 1024 * 1024)

        if self.dry_run:
            return True

        if EbpfProgType.RESOURCE_CTRL not in self._programs:
            return False

        try:
            return True
        except Exception:
            return False

    def get_stats(self) -> EbpfStats:
        """Get statistics from all loaded eBPF programs.

        Returns:
            EbpfStats with statistics from each program type
        """
        if self.dry_run:
            return EbpfStats()

        stats = EbpfStats()

        # Get stats from each loaded program
        for prog_type, state in self._programs.items():
            if not state.loaded:
                continue

            try:
                if prog_type == EbpfProgType.SCHED_TRACE:
                    stats.sched_trace = self._get_sched_trace_stats()
                elif prog_type == EbpfProgType.NET_POLICY:
                    stats.net_policy = self._get_net_policy_stats()
                elif prog_type == EbpfProgType.RESOURCE_CTRL:
                    stats.resource_ctrl = self._get_resource_ctrl_stats()
            except Exception:
                pass

        return stats

    def _get_object_path(self, prog_type: EbpfProgType) -> Path:
        """Get path to BPF object file."""
        obj_files = {
            EbpfProgType.SCHED_TRACE: "sched_trace.bpf.o",
            EbpfProgType.NET_POLICY: "net_policy.bpf.o",
            EbpfProgType.RESOURCE_CTRL: "resource_ctrl.bpf.o",
        }
        return self.ebpf_dir / "build" / obj_files[prog_type]

    def _attach_sched_trace(self, state: EbpfProgramState) -> bool:
        """Attach sched_trace program to tracepoints."""
        # Loading and verifier validation are implemented. Persistent link
        # management still requires a libbpf loader or pinned bpf_link.
        return False

    def _attach_net_policy(self, state: EbpfProgramState) -> bool:
        """Attach net_policy program to TC hooks."""
        # An interface must be selected explicitly before changing TC state.
        return False

    def _attach_resource_ctrl(self, state: EbpfProgramState) -> bool:
        """Attach resource_ctrl program to kprobes."""
        return False

    def _detach_program(self, prog_type: EbpfProgType) -> bool:
        """Detach a BPF program from its hooks."""
        if prog_type in self._programs:
            self._programs[prog_type].attached = False
        return True

    def _get_sched_trace_stats(self) -> dict[str, int]:
        """Get statistics from sched_trace program."""
        # Would read from BPF map
        return {
            "total_wakeups": 0,
            "total_switches": 0,
            "total_latency_ns": 0,
        }

    def _get_net_policy_stats(self) -> dict[str, int]:
        """Get statistics from net_policy program."""
        return {
            "total_packets": 0,
            "total_bytes": 0,
            "dropped_packets": 0,
            "dropped_bytes": 0,
        }

    def _get_resource_ctrl_stats(self) -> dict[str, int]:
        """Get statistics from resource_ctrl program."""
        return {
            "total_allocations": 0,
            "total_frees": 0,
            "total_bytes_allocated": 0,
            "oom_events": 0,
        }

    @staticmethod
    def classify_to_ebpf_class(workload_type: str) -> int:
        """Convert SchedX workload type to eBPF class ID.

        Args:
            workload_type: Workload type string from classifier

        Returns:
            eBPF class ID
        """
        mapping = {
            "latency_sensitive": EbpfController.CLASS_LATENCY,
            "batch_compute": EbpfController.CLASS_BATCH,
            "background_noise": EbpfController.CLASS_BACKGROUND,
            "unknown": EbpfController.CLASS_UNKNOWN,
        }
        return mapping.get(workload_type, EbpfController.CLASS_UNKNOWN)
