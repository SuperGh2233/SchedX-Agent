from __future__ import annotations

import os
import subprocess
from dataclasses import asdict
from dataclasses import is_dataclass
from pathlib import Path

from schedx.agent.actions import Action
from schedx.controllers.cgroup_controller import CgroupController
from schedx.controllers.process_controller import ProcessController, is_protected_control_process
from schedx.controllers.process_state import ProcessState
from schedx.state import restoration_failed


class SafeActionExecutor:
    def __init__(self, cgroup: CgroupController, processes: ProcessController | None = None) -> None:
        self.cgroup = cgroup
        self.processes = processes or ProcessController()
        self.process_state = ProcessState(
            getattr(cgroup, "rollback_file", Path(".schedx/rollback.json")).with_name("process_rollback.json"),
            getattr(cgroup, "owner", None),
            getattr(cgroup, "transaction", None),
        )

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
                if any(r.get("status") in {"failed", "unsupported"} for r in results):
                    raise RuntimeError("action failed; stopping transaction")
        except Exception as exc:
            if dry_run:
                results.append({"status": "failed", "error": str(exc), "dry_run": True})
                return results
            rolled_back = []
            for restore in (self.cgroup.rollback, self.process_state.rollback):
                try:
                    rolled_back.extend(restore())
                except Exception as rollback_exc:
                    rolled_back.append({"status": "failed", "reason": f"restore_failed: {rollback_exc}"})
            results.append(
                {
                    "status": "rollback_failed" if restoration_failed(rolled_back) else "failed_rolled_back",
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
        proc_root = getattr(self.processes, "proc_root", None)
        if proc_root is not None:
            try:
                actual = (proc_root / str(pid) / "comm").read_text().strip().lower()
                raw = (proc_root / str(pid) / "cmdline").read_bytes()
                if not raw or (comm and actual != comm):
                    return True
                comm, cmdline = actual, raw.replace(b"\0", b" ").decode(errors="replace").lower()
            except OSError:
                # Preview/fake procfs paths may be unavailable; process identity
                # is checked again by the journal before actual task changes.
                pass
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
                if not -20 <= nice_val <= 19:
                    raise ValueError("nice must be between -20 and 19")
                self.process_state.record(pid, "nice", os.getpriority(os.PRIO_PROCESS, pid))
                os.setpriority(os.PRIO_PROCESS, pid, nice_val)
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
                flags = {"SCHED_OTHER": "--other", "SCHED_BATCH": "--batch", "SCHED_IDLE": "--idle", "SCHED_FIFO": "--fifo", "SCHED_RR": "--rr"}
                if policy not in flags:
                    raise ValueError(f"unsupported scheduling policy: {policy}")
                priority = int(action.metadata.get("priority", 0))
                self.process_state.record(pid, "sched_policy", {"policy": os.sched_getscheduler(pid), "priority": os.sched_getparam(pid).sched_priority})
                subprocess.run(["chrt", flags[policy], "--pid", str(priority), str(pid)],
                               check=True, capture_output=True, text=True, timeout=5)
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
                self.process_state.record(pid, "affinity", sorted(os.sched_getaffinity(pid)))
                subprocess.run(["taskset", "-p", "-c", cpus, str(pid)],
                               check=True, capture_output=True, text=True, timeout=5)
                results.append({"action": asdict(action), "status": "ok", "pid": pid, "cpus": cpus})
            except Exception as exc:
                results.append({"action": asdict(action), "status": "failed", "pid": pid, "error": str(exc)})
        return results


def _serialize(value):
    if is_dataclass(value):
        return asdict(value)
    return value
