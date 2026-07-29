"""SCX Mapper - Maps workload classifications to sched_ext scheduling policies.

This module provides the mapping between SchedX-Agent's workload classification
and the scx_agent BPF scheduler's policy parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from schedx.controllers.scx_controller import (
    SCX_CLASS_BACKGROUND,
    SCX_CLASS_BATCH,
    SCX_CLASS_LATENCY,
    SCX_CLASS_UNKNOWN,
    SCX_WEIGHT_DEFAULTS,
    ScxController,
)
from schedx.controllers.process_controller import is_protected_control_process


@dataclass
class ScxPolicyMapping:
    """Mapping from workload type to SCX policy parameters."""
    workload_type: str
    class_id: int
    weight: int
    priority: str  # "high", "normal", "low"
    description: str


# Default mapping configuration
DEFAULT_MAPPINGS: dict[str, ScxPolicyMapping] = {
    "latency_sensitive": ScxPolicyMapping(
        workload_type="latency_sensitive",
        class_id=SCX_CLASS_LATENCY,
        weight=SCX_WEIGHT_DEFAULTS[SCX_CLASS_LATENCY],
        priority="high",
        description="Latency-sensitive tasks (nginx, redis, envoy)",
    ),
    "batch_compute": ScxPolicyMapping(
        workload_type="batch_compute",
        class_id=SCX_CLASS_BATCH,
        weight=SCX_WEIGHT_DEFAULTS[SCX_CLASS_BATCH],
        priority="normal",
        description="Batch compute tasks (gcc, make, sysbench)",
    ),
    "background_noise": ScxPolicyMapping(
        workload_type="background_noise",
        class_id=SCX_CLASS_BACKGROUND,
        weight=SCX_WEIGHT_DEFAULTS[SCX_CLASS_BACKGROUND],
        priority="low",
        description="Background noise tasks (stress-ng, openssl)",
    ),
    "unknown": ScxPolicyMapping(
        workload_type="unknown",
        class_id=SCX_CLASS_UNKNOWN,
        weight=SCX_WEIGHT_DEFAULTS[SCX_CLASS_UNKNOWN],
        priority="normal",
        description="Unknown workload type",
    ),
}


class ScxPolicyMapper:
    """Maps workload classifications to scx scheduling policies.

    This mapper takes the output from WorkloadClassifier and generates
    appropriate scx scheduling policies for each process or cgroup.
    """

    def __init__(
        self,
        controller: ScxController | None = None,
        custom_mappings: dict[str, ScxPolicyMapping] | None = None,
    ) -> None:
        self.controller = controller or ScxController()
        self.mappings = {**DEFAULT_MAPPINGS, **(custom_mappings or {})}

    def get_mapping(self, workload_type: str) -> ScxPolicyMapping:
        """Get policy mapping for a workload type.

        Args:
            workload_type: Workload type from classifier

        Returns:
            ScxPolicyMapping for the workload type
        """
        return self.mappings.get(workload_type, self.mappings["unknown"])

    def map_classification(
        self,
        classification: dict[str, Any],
        mode: str = "latency_first",
    ) -> list[dict[str, Any]]:
        """Map classification results to SCX policy actions.

        Args:
            classification: Output from WorkloadClassifier.classify_snapshot()
            mode: Optimization mode

        Returns:
            List of policy actions to apply
        """
        actions = []
        groups = classification.get("groups", {})

        for workload_type, processes in groups.items():
            if workload_type == "unknown":
                continue

            mapping = self.get_mapping(workload_type)

            # Adjust weight based on mode
            weight = self._adjust_weight(mapping.weight, workload_type, mode)

            for proc in processes:
                pid = proc.get("pid")
                if pid is None:
                    continue
                if is_protected_control_process(
                    str(proc.get("comm", "")),
                    str(proc.get("cmdline", "")),
                ):
                    continue

                actions.append({
                    "type": "set_task_policy",
                    "pid": pid,
                    "class_id": mapping.class_id,
                    "weight": weight,
                    "workload_type": workload_type,
                    "comm": proc.get("comm", ""),
                    "reason": proc.get("reason", ""),
                })

        return actions

    def apply_policies(
        self,
        classification: dict[str, Any],
        mode: str = "latency_first",
    ) -> dict[str, Any]:
        """Apply scheduling policies based on classification.

        Args:
            classification: Output from WorkloadClassifier.classify_snapshot()
            mode: Optimization mode

        Returns:
            Results of policy application
        """
        actions = self.map_classification(classification, mode)
        results = []

        for action in actions:
            try:
                if action["type"] == "set_task_policy":
                    success = self.controller.set_task_policy(
                        pid=action["pid"],
                        class_id=action["class_id"],
                        weight=action["weight"],
                    )
                    results.append({
                        **action,
                        "success": success,
                    })
            except Exception as e:
                results.append({
                    **action,
                    "success": False,
                    "error": str(e),
                })

        return {
            "mode": mode,
            "total_actions": len(actions),
            "successful": sum(1 for r in results if r.get("success")),
            "failed": sum(1 for r in results if not r.get("success")),
            "results": results,
        }

    def _adjust_weight(
        self,
        base_weight: int,
        workload_type: str,
        mode: str,
    ) -> int:
        """Adjust weight based on optimization mode.

        Args:
            base_weight: Base weight from mapping
            workload_type: Type of workload
            mode: Optimization mode

        Returns:
            Adjusted weight
        """
        if mode == "latency_first":
            # Boost latency-sensitive, reduce background
            if workload_type == "latency_sensitive":
                return min(10000, base_weight * 2)
            elif workload_type == "background_noise":
                return max(1, base_weight // 2)

        elif mode == "throughput_first":
            # Boost batch, reduce latency boost
            if workload_type == "batch_compute":
                return min(10000, base_weight * 2)
            elif workload_type == "latency_sensitive":
                return max(1, base_weight // 2)
            elif workload_type == "background_noise":
                return max(300, base_weight)

        elif mode == "balanced":
            # Use default weights
            pass

        elif mode == "isolate_background":
            # Heavily penalize background
            if workload_type == "background_noise":
                return max(1, base_weight // 4)

        return base_weight


def create_mapper_for_mode(mode: str) -> ScxPolicyMapper:
    """Create a policy mapper configured for a specific optimization mode.

    Args:
        mode: Optimization mode

    Returns:
        Configured ScxPolicyMapper
    """
    return ScxPolicyMapper()
