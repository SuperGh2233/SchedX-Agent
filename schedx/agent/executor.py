from __future__ import annotations

import os
import subprocess
from dataclasses import asdict
from dataclasses import is_dataclass

from schedx.agent.actions import Action
from schedx.controllers.cgroup_controller import CgroupController
from schedx.controllers.process_controller import ProcessController, is_protected_control_process


class SafeActionExecutor:
    def __init__(self, cgroup: CgroupController, processes: ProcessController | None = None) -> None:
        self.cgroup = cgroup
        self.processes = processes or ProcessController()

    def execute(self, actions: list[Action], dry_run: bool | None = None) -> list[dict]:
        if dry_run is None:
            dry_run = bool(self.cgroup.dry_run)
        results: list[dict] = []
        try:
            for action in actions:
                if action.action == "set_cgroup_cpu_weight":
                    results.extend(self._set_weight(action, dry_run))
                elif action.action == "set_cgroup_cpu_max":
                    results.extend(self._set_max(action, dry_run))
                elif action.action == "set_cpuset_cpus":
                    results.extend(self._set_cpuset(action, dry_run))
                elif action.action == "set_nice":
                    results.extend(self._set_nice(action, dry_run))
                elif action.action == "set_sched_policy":
                    results.extend(self._set_sched_policy(action, dry_run))
                elif action.action == "set_affinity":
                    results.extend(self._set_affinity(action, dry_run))
                elif action.action == "move_pid_to_cgroup":
                    if dry_run:
                        results.append({"action": asdict(action), "status": "dry_run", "target": action.target})
                        continue
                    self.cgroup.add_pid(action.target, int(action.value or 0))
                    results.append({"action": asdict(action), "status": "ok"})
                elif action.action in {"start_scx_scheduler", "stop_scx_scheduler", "create_cgroup"}:
                    results.append({"action": asdict(action), "status": "skipped_phase1"})
                else:
                    results.append({"action": asdict(action), "status": "unsupported"})
        except Exception as exc:
            rolled_back = self.cgroup.rollback()
            results.append(
                {
                    "status": "failed_rolled_back",
                    "error": str(exc),
                    "rolled_back": [_serialize(entry) for entry in rolled_back],
                }
            )
        return results

    def _target_pids(self, action: Action) -> list[int]:
        if action.target_type == "pid":
            return [int(action.target)]
        if action.target_type == "process_name":
            return self.processes.find_by_name(action.target)
        return []

    def _is_protected(self, action: Action, pid: int) -> bool:
        comm = str(action.metadata.get("comm", "")).lower()
        cmdline = str(action.metadata.get("cmdline", "")).lower()
        return is_protected_control_process(comm, cmdline)

    def _set_weight(self, action: Action, dry_run: bool) -> list[dict]:
        results: list[dict] = []
        pids = self._target_pids(action)
        if not pids:
            if dry_run:
                return [{"action": asdict(action), "status": "dry_run", "target": action.target, "note": "no_target_pid"}]
            return [{"action": asdict(action), "status": "no_target_pid", "target": action.target}]
        for pid in pids:
            group = f"pid-{pid}"
            if self._is_protected(action, pid):
                results.append({"action": asdict(action), "status": "skipped", "pid": pid, "reason": "protected"})
                continue
            if dry_run:
                results.append({"action": asdict(action), "status": "dry_run", "pid": pid, "group": group})
                continue
            self.cgroup.create_group(group)
            self.cgroup.add_pid(group, pid)
            self.cgroup.set_cpu_weight(group, int(action.value or 100))
            results.append({"action": asdict(action), "status": "ok", "pid": pid, "group": group})
        return results

    def _set_max(self, action: Action, dry_run: bool) -> list[dict]:
        results: list[dict] = []
        pids = self._target_pids(action)
        if not pids:
            if dry_run:
                return [{"action": asdict(action), "status": "dry_run", "target": action.target, "note": "no_target_pid"}]
            return [{"action": asdict(action), "status": "no_target_pid", "target": action.target}]
        for pid in pids:
            group = f"pid-{pid}"
            if self._is_protected(action, pid):
                results.append({"action": asdict(action), "status": "skipped", "pid": pid, "reason": "protected"})
                continue
            if dry_run:
                results.append({"action": asdict(action), "status": "dry_run", "pid": pid, "group": group})
                continue
            self.cgroup.create_group(group)
            self.cgroup.add_pid(group, pid)
            self.cgroup.set_cpu_max(group, str(action.value or "max"))
            results.append({"action": asdict(action), "status": "ok", "pid": pid, "group": group})
        return results

    def _set_cpuset(self, action: Action, dry_run: bool) -> list[dict]:
        results: list[dict] = []
        pids = self._target_pids(action)
        if not pids:
            if dry_run:
                return [{"action": asdict(action), "status": "dry_run", "target": action.target, "note": "no_target_pid"}]
            return [{"action": asdict(action), "status": "no_target_pid", "target": action.target}]
        for pid in pids:
            group = f"pid-{pid}"
            if self._is_protected(action, pid):
                results.append({"action": asdict(action), "status": "skipped", "pid": pid, "reason": "protected"})
                continue
            if dry_run:
                results.append({"action": asdict(action), "status": "dry_run", "pid": pid, "group": group, "cpus": str(action.value)})
                continue
            self.cgroup.create_group(group)
            self.cgroup.set_cpuset_mems(group, "0")
            self.cgroup.set_cpuset_cpus(group, str(action.value))
            self.cgroup.add_pid(group, pid)
            results.append({"action": asdict(action), "status": "ok", "pid": pid, "group": group, "cpus": str(action.value)})
        return results

    def _set_nice(self, action: Action, dry_run: bool) -> list[dict]:
        results: list[dict] = []
        pids = self._target_pids(action)
        if not pids:
            return [{"action": asdict(action), "status": "no_target_pid", "target": action.target}]
        nice_val = int(action.value or 0)
        for pid in pids:
            if self._is_protected(action, pid):
                results.append({"action": asdict(action), "status": "skipped", "pid": pid, "reason": "protected"})
                continue
            if dry_run:
                results.append({"action": asdict(action), "status": "dry_run", "pid": pid, "nice": nice_val})
                continue
            try:
                os.sched_set_priority(0, pid)  # noop to check pid exists
            except (OSError, ProcessLookupError):
                pass
            try:
                subprocess.run(["renice", str(nice_val), "-p", str(pid)],
                               check=False, capture_output=True, timeout=5)
                results.append({"action": asdict(action), "status": "ok", "pid": pid, "nice": nice_val})
            except Exception as exc:
                results.append({"action": asdict(action), "status": "failed", "pid": pid, "error": str(exc)})
        return results

    def _set_sched_policy(self, action: Action, dry_run: bool) -> list[dict]:
        results: list[dict] = []
        pids = self._target_pids(action)
        if not pids:
            return [{"action": asdict(action), "status": "no_target_pid", "target": action.target}]
        policy = str(action.value or "SCHED_OTHER")
        for pid in pids:
            if self._is_protected(action, pid):
                results.append({"action": asdict(action), "status": "skipped", "pid": pid, "reason": "protected"})
                continue
            if dry_run:
                results.append({"action": asdict(action), "status": "dry_run", "pid": pid, "policy": policy})
                continue
            try:
                subprocess.run(["chrt", "--pid", policy.lower().replace("sched_", ""), str(pid)],
                               check=False, capture_output=True, timeout=5)
                results.append({"action": asdict(action), "status": "ok", "pid": pid, "policy": policy})
            except Exception as exc:
                results.append({"action": asdict(action), "status": "failed", "pid": pid, "error": str(exc)})
        return results

    def _set_affinity(self, action: Action, dry_run: bool) -> list[dict]:
        results: list[dict] = []
        pids = self._target_pids(action)
        if not pids:
            return [{"action": asdict(action), "status": "no_target_pid", "target": action.target}]
        cpus = str(action.value)
        for pid in pids:
            if self._is_protected(action, pid):
                results.append({"action": asdict(action), "status": "skipped", "pid": pid, "reason": "protected"})
                continue
            if dry_run:
                results.append({"action": asdict(action), "status": "dry_run", "pid": pid, "cpus": cpus})
                continue
            try:
                subprocess.run(["taskset", "-p", "-c", cpus, str(pid)],
                               check=False, capture_output=True, timeout=5)
                results.append({"action": asdict(action), "status": "ok", "pid": pid, "cpus": cpus})
            except Exception as exc:
                results.append({"action": asdict(action), "status": "failed", "pid": pid, "error": str(exc)})
        return results


def _serialize(value):
    if is_dataclass(value):
        return asdict(value)
    return value
