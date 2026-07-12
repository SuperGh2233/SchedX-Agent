from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ActionName = Literal[
    "create_cgroup",
    "move_pid_to_cgroup",
    "set_cgroup_cpu_weight",
    "set_cgroup_cpu_max",
    "set_cpuset_cpus",
    "set_affinity",
    "set_nice",
    "set_sched_policy",
    "start_scx_scheduler",
    "stop_scx_scheduler",
]

RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class Action:
    action: ActionName
    target: str
    target_type: str
    value: int | str | None = None
    reason: str = ""
    risk_level: RiskLevel = "low"
    rollback_enabled: bool = True
    metadata: dict[str, str | int] = field(default_factory=dict)

