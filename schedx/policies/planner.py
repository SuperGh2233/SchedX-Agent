from __future__ import annotations

from schedx.agent.actions import Action
from schedx.controllers.process_controller import is_protected_control_process


class PolicyPlanner:
    def plan(
        self,
        mode: str,
        target: str,
        classification: dict,
        topology: dict | None = None,
        parameters: dict | None = None,
    ) -> list[Action]:
        parameters = parameters or {}
        if mode == "latency_first":
            return self._latency_first(target, classification, topology, parameters)
        if mode == "throughput_first":
            return self._throughput_first(target, classification, topology, parameters)
        if mode == "isolate_background":
            return self._isolate_background(target, classification, topology, parameters)
        if mode == "balanced":
            return [Action("set_cgroup_cpu_weight", target, "process_name", int(parameters.get("cpu_weight", 500)), "balanced default")]
        if mode == "rollback_to_default":
            return []
        raise ValueError(f"unsupported mode: {mode}")

    def _latency_first(
        self, target: str, classification: dict, topology: dict | None, parameters: dict
    ) -> list[Action]:
        actions: list[Action] = []
        target_weight = int(parameters.get("cpu_weight", 10000))
        background_weight = int(parameters.get("cpu_weight_bg", 10))
        background_max = str(parameters.get("cpu_max_bg", "10000 100000"))

        # Target: maximum cpu.weight
        actions.append(Action(
            "set_cgroup_cpu_weight", target, "process_name", target_weight,
            "protect latency-sensitive workload", "medium",
        ))
        _, efficiency_mask = _isolation_masks(topology)

        # Background: minimum weight + strict cpu.max quota
        for proc in classification.get("groups", {}).get("background_noise", []):
            pid = str(proc["pid"])
            comm = proc.get("comm", "")
            actions.append(Action(
                "set_cgroup_cpu_weight", pid, "pid", background_weight,
                "apply adaptive background weight", "medium",
                metadata={"comm": comm},
            ))
            actions.append(Action(
                "set_cgroup_cpu_max", pid, "pid", background_max,
                "apply adaptive background CPU quota", "medium",
                metadata={"comm": comm},
            ))
            if efficiency_mask:
                actions.append(Action(
                    "set_cpuset_cpus", pid, "pid", efficiency_mask,
                    "pin background workload away from latency CPUs", "medium",
                    metadata={"comm": comm},
                ))

        return actions

    def _throughput_first(
        self, target: str, classification: dict, topology: dict | None, parameters: dict
    ) -> list[Action]:
        actions = [
            Action("set_cgroup_cpu_weight", target, "process_name", int(parameters.get("cpu_weight", 9000)),
                   "favor batch throughput", "medium"),
            Action("set_cgroup_cpu_max", target, "process_name", str(parameters.get("cpu_max", "max 100000")),
                   "full CPU quota for batch", "low"),
        ]
        return actions

    def _isolate_background(
        self, target: str, classification: dict, topology: dict | None, parameters: dict
    ) -> list[Action]:
        actions: list[Action] = []
        background_weight = int(parameters.get("cpu_weight_bg", 50))
        background_max = str(parameters.get("cpu_max_bg", "25000 100000"))
        candidates = classification.get("groups", {}).get("background_noise", [])
        for proc in candidates:
            match = match_isolation_target(proc, target)
            if not match["matched"]:
                continue
            pid = str(proc["pid"])
            comm = proc.get("comm", "")
            actions.append(Action(
                "set_cgroup_cpu_weight", pid, "pid", background_weight,
                "isolate background workload", "medium",
                metadata={"comm": comm, "matched_by": match["matched_by"]},
            ))
            actions.append(Action(
                "set_cgroup_cpu_max", pid, "pid", background_max,
                "apply adaptive background CPU quota", "medium",
                metadata={"comm": comm},
            ))
            _, efficiency_mask = _isolation_masks(topology)
            if efficiency_mask:
                actions.append(Action(
                    "set_cpuset_cpus", pid, "pid", efficiency_mask,
                    "pin background workload to isolated CPUs", "medium",
                    metadata={"comm": comm},
                ))
        return actions


def _isolation_masks(topology: dict | None) -> tuple[str, str]:
    if not topology or int(topology.get("total_cpus", 0) or 0) < 4:
        return "", ""
    performance = str(topology.get("performance_mask", ""))
    efficiency = str(topology.get("efficiency_mask", ""))
    if not performance or not efficiency or performance == efficiency:
        return "", ""
    return performance, efficiency


def match_isolation_target(proc: dict, target: str) -> dict[str, str | bool]:
    comm = str(proc.get("comm", "")).lower()
    cmdline = str(proc.get("cmdline", "")).lower()
    target = target.lower()

    if is_protected_control_process(comm, cmdline):
        return {"matched": False, "matched_by": "", "reason": "protected_control_process"}

    if target == "stress-ng":
        if comm in {"stress-ng", "stress-ng-cpu"}:
            return {"matched": True, "matched_by": "comm_exact", "reason": "stress-ng comm exact match"}
        if "stress-ng" in cmdline:
            return {"matched": True, "matched_by": "cmdline_contains", "reason": "stress-ng cmdline match"}
        return {"matched": False, "matched_by": "", "reason": "target_not_matched"}

    if comm == target:
        return {"matched": True, "matched_by": "comm_exact", "reason": "target comm exact match"}
    if target and target in cmdline:
        return {"matched": True, "matched_by": "cmdline_contains", "reason": "target cmdline match"}
    return {"matched": False, "matched_by": "", "reason": "target_not_matched"}
