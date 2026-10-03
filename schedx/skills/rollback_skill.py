from __future__ import annotations

import json
from dataclasses import asdict
from dataclasses import is_dataclass

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.controllers.cgroup_controller import CgroupController
from schedx.controllers.scx_controller import ScxController
from schedx.scx_daemon import ScxDaemonClient
from schedx.skills.ebpf_skill import EbpfCleanupSkill


class RollbackSkill:
    name = "rollback"
    description = "Rollback cgroup changes and restore previous state."

    def run(self, context: AgentContext) -> SkillResult:
        scx_entries = self._rollback_scx(context)
        cgroup = CgroupController(dry_run=context.dry_run)
        entries = cgroup.rollback()
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
            "ebpf_cleanup": ebpf_result.data.get("results", {}),
        }
        context.data["rollback"] = rollback_evidence

        return SkillResult(
            ebpf_result.ok,
            f"rollback completed: {len(restored_settings)} settings restored, "
            f"{len(removed_groups)} groups removed, {len(skipped)} skipped; "
            f"{ebpf_result.message}",
            rollback_evidence,
        )

    def _rollback_scx(self, context: AgentContext) -> list[dict]:
        payload = context.data.get("scx_rollback")
        if not isinstance(payload, dict) and context.scx_rollback_file.exists():
            try:
                payload = json.loads(
                    context.scx_rollback_file.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                payload = None
        if not isinstance(payload, dict):
            return []

        source = str(payload.get("source", ""))
        pids = [int(pid) for pid in payload.get("pids", [])]
        if context.dry_run:
            return [
                {"source": source, "pid": pid, "status": "dry_run"}
                for pid in pids
            ]

        results: list[dict] = []
        if source == "persistent_scx_daemon":
            client = ScxDaemonClient()
            if not client.is_available():
                return [
                    {
                        "source": source,
                        "status": "skipped",
                        "reason": "scx_daemon_unavailable",
                    }
                ]
            for pid in pids:
                try:
                    removed = client.remove_task_policy(pid)
                    results.append(
                        {
                            "source": source,
                            "pid": pid,
                            "status": "removed" if removed else "already_absent",
                        }
                    )
                except (OSError, RuntimeError) as exc:
                    results.append(
                        {
                            "source": source,
                            "pid": pid,
                            "status": "skipped",
                            "reason": str(exc),
                        }
                    )
        elif source == "standalone_scx":
            controller = context.data.get("_scx_controller")
            if controller is None:
                if ScxController().state() != "enabled":
                    results.append(
                        {"source": source, "status": "already_inactive"}
                    )
                else:
                    results.append(
                        {
                            "source": source,
                            "status": "skipped",
                            "reason": "standalone_controller_unavailable",
                        }
                    )
            else:
                for pid in pids:
                    try:
                        removed = controller.remove_task_policy(pid)
                        results.append(
                            {
                                "source": source,
                                "pid": pid,
                                "status": "removed" if removed else "already_absent",
                            }
                        )
                    except (OSError, RuntimeError) as exc:
                        results.append(
                            {
                                "source": source,
                                "pid": pid,
                                "status": "skipped",
                                "reason": str(exc),
                            }
                        )
                controller.stop_scheduler()

        if not any(entry.get("status") == "skipped" for entry in results):
            context.scx_rollback_file.unlink(missing_ok=True)
        return results


def _serialize(value):
    if is_dataclass(value):
        return asdict(value)
    return value
