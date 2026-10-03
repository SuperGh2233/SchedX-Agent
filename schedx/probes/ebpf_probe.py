"""eBPF Probe - Real implementation of eBPF hooks for SchedX-Agent.

This module provides eBPF-based probes for:
- Scheduling latency tracing
- Network policy enforcement
- Resource control monitoring
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from schedx.controllers.ebpf_controller import (
    EbpfController,
    EbpfProgType,
    EbpfStats,
)


class EbpfHook:
    """Base class for eBPF hooks."""

    name = "base-ebpf-hook"

    def __init__(self, controller: EbpfController | None = None) -> None:
        self.controller = controller or EbpfController()
        self._loaded = False

    def is_available(self) -> bool:
        """Check if this eBPF hook is available."""
        return self.controller.is_available()

    def load(self) -> bool:
        """Load the eBPF program."""
        if not self.is_available():
            return False
        prog_type = self._get_prog_type()
        self._loaded = self.controller.load_program(prog_type)
        return self._loaded

    def attach(self) -> bool:
        """Attach the eBPF program to hooks."""
        if not self._loaded:
            return False
        prog_type = self._get_prog_type()
        return self.controller.attach_program(prog_type)

    def unload(self) -> bool:
        """Unload the eBPF program."""
        if not self._loaded:
            return True
        prog_type = self._get_prog_type()
        self._loaded = not self.controller.unload_program(prog_type)
        return not self._loaded

    def snapshot(self) -> dict[str, Any]:
        """Get current state snapshot."""
        return {
            "available": self.is_available(),
            "loaded": self._loaded,
            "name": self.name,
        }

    def _get_prog_type(self) -> EbpfProgType:
        """Get the eBPF program type for this hook."""
        raise NotImplementedError


class SchedulerTraceHook(EbpfHook):
    """eBPF hook for scheduling latency tracing.

    This hook traces sched_switch and sched_wakeup events to
    measure scheduling latency per task.
    """

    name = "scheduler-trace"

    def __init__(self, controller: EbpfController | None = None) -> None:
        super().__init__(controller)
        self._stats: EbpfStats | None = None

    def _get_prog_type(self) -> EbpfProgType:
        return EbpfProgType.SCHED_TRACE

    def snapshot(self) -> dict[str, Any]:
        """Get scheduling trace statistics."""
        base = super().snapshot()

        if not self._loaded:
            base["message"] = "Scheduler trace not loaded"
            return base

        try:
            stats = self.controller.get_stats()
            if stats.sched_trace:
                base["stats"] = stats.sched_trace
                base["total_wakeups"] = stats.sched_trace.get("total_wakeups", 0)
                base["total_switches"] = stats.sched_trace.get("total_switches", 0)
                base["total_latency_ns"] = stats.sched_trace.get("total_latency_ns", 0)

                # Calculate average latency
                total_wakeups = stats.sched_trace.get("total_wakeups", 0)
                if total_wakeups > 0:
                    base["avg_latency_ns"] = (
                        stats.sched_trace.get("total_latency_ns", 0) / total_wakeups
                    )
                else:
                    base["avg_latency_ns"] = 0
        except Exception as e:
            base["error"] = str(e)

        return base

    def get_task_latencies(self) -> dict[int, dict[str, Any]]:
        """Get per-task latency information.

        Returns:
            Dictionary mapping PID to latency info
        """
        # This would read from the task_info_map BPF map
        # For now, return empty dict
        return {}


class NetworkPolicyHook(EbpfHook):
    """eBPF hook for network policy enforcement.

    This hook uses a cgroup v2 egress program to enforce network
    bandwidth limits per workload class.
    """

    name = "network-policy"

    def __init__(self, controller: EbpfController | None = None) -> None:
        super().__init__(controller)
        self._stats: EbpfStats | None = None

    def _get_prog_type(self) -> EbpfProgType:
        return EbpfProgType.NET_POLICY

    def snapshot(self) -> dict[str, Any]:
        """Get network policy statistics."""
        base = super().snapshot()

        if not self._loaded:
            base["message"] = "Network policy not loaded"
            return base

        try:
            stats = self.controller.get_stats()
            if stats.net_policy:
                base["stats"] = stats.net_policy
                base["total_packets"] = stats.net_policy.get("total_packets", 0)
                base["total_bytes"] = stats.net_policy.get("total_bytes", 0)
                base["dropped_packets"] = stats.net_policy.get("dropped_packets", 0)
                base["dropped_bytes"] = stats.net_policy.get("dropped_bytes", 0)

                # Calculate drop rate
                total = stats.net_policy.get("total_packets", 0)
                dropped = stats.net_policy.get("dropped_packets", 0)
                if total > 0:
                    base["drop_rate"] = dropped / total
                else:
                    base["drop_rate"] = 0.0
        except Exception as e:
            base["error"] = str(e)

        return base

    def set_policy(
        self,
        pid: int,
        class_id: int,
        rate_limit: int | None = None,
    ) -> bool:
        """Set network policy for a task.

        Args:
            pid: Process ID
            class_id: Network class (CLASS_*)
            rate_limit: Rate limit in bytes/sec

        Returns:
            True if policy set successfully
        """
        return self.controller.update_net_policy(pid, class_id, rate_limit)


class SecurityPolicyHook(EbpfHook):
    """Default-allow BPF LSM policy scoped to cgroup and executable."""

    name = "security-policy"

    def _get_prog_type(self) -> EbpfProgType:
        return EbpfProgType.SECURITY_POLICY

    def snapshot(self) -> dict[str, Any]:
        base = super().snapshot()
        base["bpf_lsm"] = self.controller.security_lsm_available()
        if not self._loaded:
            base["message"] = "Security policy not loaded"
            return base
        stats = self.controller.get_stats().security_policy
        if stats:
            base["stats"] = stats
        return base

    def set_policy(
        self,
        cgroup_id: int,
        executable: str | Path,
        *,
        deny_exec: bool = False,
        audit_only: bool = True,
    ) -> bool:
        return self.controller.update_security_policy(
            cgroup_id,
            executable,
            deny_exec=deny_exec,
            audit_only=audit_only,
        )


class ResourceControlHook(EbpfHook):
    """Observe cgroup resource-policy activity; cgroup v2 enforces limits."""

    name = "resource-control"

    def __init__(self, controller: EbpfController | None = None) -> None:
        super().__init__(controller)
        self._stats: EbpfStats | None = None

    def _get_prog_type(self) -> EbpfProgType:
        return EbpfProgType.RESOURCE_CTRL

    def snapshot(self) -> dict[str, Any]:
        """Get resource control statistics."""
        base = super().snapshot()

        if not self._loaded:
            base["message"] = "Resource control not loaded"
            return base

        try:
            stats = self.controller.get_stats()
            if stats.resource_ctrl:
                base["stats"] = stats.resource_ctrl
                base["sched_switches"] = stats.resource_ctrl.get("sched_switches", 0)
                base["policy_hits"] = stats.resource_ctrl.get("policy_hits", 0)
                base["process_exits"] = stats.resource_ctrl.get("process_exits", 0)
        except Exception as e:
            base["error"] = str(e)

        return base

    def set_policy(
        self,
        cgroup_id: int,
        class_id: int,
        memory_limit: int | None = None,
    ) -> bool:
        """Set resource policy for a cgroup.

        Args:
            cgroup_id: Cgroup ID
            class_id: Resource class (CLASS_*)
            memory_limit: Memory limit in bytes

        Returns:
            True if policy set successfully
        """
        return self.controller.update_resource_policy(cgroup_id, class_id, memory_limit)


class EbpfProbe:
    """Combined eBPF probe for all hook types.

    This class provides a unified interface for all eBPF hooks.
    """

    def __init__(self, dry_run: bool = True) -> None:
        self.controller = EbpfController(dry_run=dry_run)
        self.scheduler_trace = SchedulerTraceHook(self.controller)
        self.network_policy = NetworkPolicyHook(self.controller)
        self.security_policy = SecurityPolicyHook(self.controller)
        self.resource_control = ResourceControlHook(self.controller)

    def _hooks(self) -> tuple[EbpfHook, ...]:
        return (
            self.scheduler_trace,
            self.network_policy,
            self.resource_control,
            self.security_policy,
        )

    def is_available(self) -> bool:
        """Check if eBPF is available."""
        return self.controller.is_available()

    def load_all(self) -> dict[str, bool]:
        """Load all eBPF programs.

        Returns:
            Dictionary mapping hook name to load success
        """
        results = {}
        for hook in self._hooks():
            try:
                results[hook.name] = hook.load()
            except Exception:
                results[hook.name] = False
        return results

    def attach_all(self) -> dict[str, bool]:
        """Attach all loaded eBPF programs.

        Returns:
            Dictionary mapping hook name to attach success
        """
        results = {}
        for hook in self._hooks():
            try:
                results[hook.name] = hook.attach()
            except Exception:
                results[hook.name] = False
        return results

    def unload_all(self) -> dict[str, bool]:
        """Unload all eBPF programs.

        Returns:
            Dictionary mapping hook name to unload success
        """
        results = {}
        for hook in reversed(self._hooks()):
            try:
                results[hook.name] = hook.unload()
            except Exception:
                results[hook.name] = False
        return results

    def cleanup_pinned(self) -> dict[str, bool]:
        """Clean eBPF state left active by a previous Agent process."""
        return self.controller.cleanup_pinned()

    def snapshot(self) -> dict[str, Any]:
        """Get combined snapshot from all hooks."""
        return {
            "available": self.is_available(),
            "status": self.controller.get_status(),
            "hooks": {
                "scheduler_trace": self.scheduler_trace.snapshot(),
                "network_policy": self.network_policy.snapshot(),
                "security_policy": self.security_policy.snapshot(),
                "resource_control": self.resource_control.snapshot(),
            },
        }
