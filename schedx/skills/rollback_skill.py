from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import is_dataclass
from pathlib import Path

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.controllers.cgroup_controller import CgroupController
from schedx.controllers.scx_controller import ScxController
from schedx.scx_daemon import ScxDaemonClient
from schedx.controllers.process_state import ProcessState
from schedx.state import atomic_json, process_start_time, restoration_failed, state_lock
from schedx.skills.ebpf_skill import EbpfCleanupSkill, EbpfPolicySkill
from schedx.probes.ebpf_probe import EbpfProbe


class RollbackSkill:
    name = "rollback"
    description = "Rollback cgroup changes and restore previous state."

    def run(self, context: AgentContext) -> SkillResult:
        try:
            scx_entries = self._rollback_scx(context)
        except Exception as exc:
            scx_entries = [{"status": "failed", "reason": f"restore_failed: {exc}"}]
        cgroup = CgroupController(dry_run=context.dry_run, rollback_file=context.rollback_file,
                                  owner=context.data.get("transaction_owner"), transaction=context.data.get("transaction_id"))
        try:
            entries = cgroup.rollback()
        except Exception as exc:
            entries = [{"status": "failed", "reason": f"restore_failed: {exc}"}]
        try:
            process_entries = ProcessState(context.state_dir / "process_rollback.json", context.data.get("transaction_owner"), context.data.get("transaction_id")).rollback(context.dry_run)
        except Exception as exc:
            process_entries = [{"status": "failed", "reason": f"restore_failed: {exc}"}]
        accepted = context.data.get("accepted_classification")
        active_probe = context.data.get("_ebpf_probe")
        if accepted is not None and isinstance(active_probe, EbpfProbe):
            try:
                live, expired = self._live_accepted(accepted, active_probe.controller.proc_root)
                previous = EbpfPolicySkill()._apply_policies(live, active_probe.controller)
                expired_ids = {row.get("cgroup_id") for row in context.data.get("accepted_ebpf_policy_results", [])
                               if row.get("pid") in expired and row.get("cgroup_id")}
                kept_ids = set(context.data.get("accepted_cgroup_ids", [])) - expired_ids
                new_ids = ({row.get("cgroup_id") for row in context.data.get("ebpf_policy_results", []) if row.get("cgroup_id")} - kept_ids) | expired_ids
                removed = [active_probe.controller.remove_cgroup_policies(cgroup_id) for cgroup_id in new_ids]
                ok = all(row.get("success") for row in previous) and all(removed)
                ebpf_result = SkillResult(ok, "restored live previously accepted eBPF policy", {"results": {"previous_policy_restored": ok,
                                         "expired_pids": sorted(expired), "expired_cgroup_ids_removed": sorted(expired_ids)}})
            except Exception as exc:
                ebpf_result = SkillResult(False, f"failed to restore previous eBPF policy: {exc}", {"results": {}})
        else:
            ebpf_result = EbpfCleanupSkill().run(context)

        context.data["rollback_results"] = entries
        context.data["scx_rollback_results"] = scx_entries

        restored_settings = [e for e in entries if is_dataclass(e)]
        removed_groups = [e for e in entries if isinstance(e, dict) and e.get("status") == "removed"]
        skipped = [e for e in entries if isinstance(e, dict) and e.get("status") == "skipped"]
        rollback_evidence = {
            "restored": len(restored_settings),
            "groups_removed": len(removed_groups),
            "skipped": len(skipped),
            "entries": [_serialize(e) for e in entries],
            "scx_entries": scx_entries,
            "process_entries": process_entries,
            "ebpf_cleanup": ebpf_result.data.get("results", {}),
        }
        context.data["rollback"] = rollback_evidence

        return SkillResult(
            ebpf_result.ok and not restoration_failed(entries + scx_entries + process_entries),
            f"rollback completed: {len(restored_settings)} settings restored, "
            f"{len(removed_groups)} groups removed, {len(skipped)} skipped; "
            f"{ebpf_result.message}",
            rollback_evidence,
        )

    @staticmethod
    def _live_accepted(classification: dict, proc_root: Path) -> tuple[dict, set[int]]:
        live = {key: value for key, value in classification.items() if key != "groups"}
        live["groups"] = {}
        expired = set()
        for name, processes in classification.get("groups", {}).items():
            kept = []
            for process in processes:
                pid = int(process["pid"])
                current = process_start_time(pid, proc_root)
                if current is None:
                    if (proc_root / str(pid)).exists():
                        raise RuntimeError(f"cannot verify identity of previously accepted PID {pid}")
                    expired.add(pid)
                    continue
                identity = process.get("start_time")
                if identity and str(identity) != current:
                    expired.add(pid)
                    continue
                kept.append(process)
            live["groups"][name] = kept
        return live, expired

    def _rollback_scx(self, context: AgentContext) -> list[dict]:
        with state_lock(context.scx_rollback_file):
            payload = context.data.get("scx_rollback")
            if context.scx_rollback_file.exists():
                payload = json.loads(context.scx_rollback_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return []
            owner = context.data.get("transaction_owner")
            if owner and payload.get("owner") and payload["owner"] != owner:
                return []
            source = str(payload.get("source", ""))
            records = payload.get("entries", [{"pid": int(pid), "previous": None} for pid in payload.get("pids", [])])
            if context.dry_run:
                return [{"source": source, **row, "status": "dry_run"} for row in records]
            client = ScxDaemonClient() if source == "persistent_scx_daemon" else context.data.get("_scx_controller")
            if client is None:
                if ScxController().state() == "enabled":
                    return [{"source": source, "status": "failed", "reason": "standalone_controller_unavailable"}]
                context.scx_rollback_file.unlink(missing_ok=True)
                return [{"source": source, "status": "already_inactive"}]
            if source == "persistent_scx_daemon" and not client.is_available():
                return [{"source": source, "status": "failed", "reason": "scx_daemon_unavailable"}]
            results, pending = [], []
            blocked = set()
            for row in (reversed(records) if "entries" in payload else records):
                if context.data.get("transaction_id") and row.get("transaction") != context.data["transaction_id"]:
                    pending.append(row)
                    continue
                scope = row.get("scope", "task")
                id_field = "pid" if scope == "task" else "cgroup_id"
                pid = int(row[id_field])
                key = (scope, pid)
                if key in blocked:
                    pending.append(row)
                    continue
                if row.get("pid_start") and process_start_time(pid) != row["pid_start"]:
                    results.append({"source": source, "pid": pid, "status": "skipped", "reason": "target_process_exited_or_reused"})
                    continue
                try:
                    previous = row.get("previous")
                    if previous is None:
                        remove = client.remove_task_policy if scope == "task" else client.remove_cgroup_policy
                        success = remove(pid)
                    else:
                        update = client.set_task_policy if scope == "task" else client.set_cgroup_policy
                        success = update(pid, int(previous["class_id"]), int(previous["weight"]))
                    if not success:
                        raise RuntimeError("scheduler did not acknowledge policy restoration")
                    results.append({"source": source, id_field: pid, "status": "restored" if previous else "removed"})
                except (OSError, RuntimeError) as exc:
                    pending.append(row)
                    blocked.add(key)
                    results.append({"source": source, "pid": pid, "status": "failed", "reason": str(exc)})
            stop_pending = False
            if source == "standalone_scx" and not pending:
                try:
                    stop_pending = not client.stop_scheduler()
                except Exception as exc:
                    stop_pending = True
                    results.append({"source": source, "status": "failed", "reason": f"scheduler_stop_failed: {exc}"})
                if stop_pending and not any(row.get("reason", "").startswith("scheduler_stop_failed") for row in results):
                    results.append({"source": source, "status": "failed", "reason": "scheduler_stop_failed"})
            if pending or stop_pending:
                payload["entries"] = list(reversed(pending))
                payload["pids"] = [row["pid"] for row in pending if "pid" in row]
                payload["scheduler_stop_pending"] = stop_pending
                atomic_json(context.scx_rollback_file, payload)
                context.data["scx_rollback"] = payload
            else:
                context.scx_rollback_file.unlink(missing_ok=True)
                context.data.pop("scx_rollback", None)
            return results


def _serialize(value):
    if is_dataclass(value):
        return asdict(value)
    return value
