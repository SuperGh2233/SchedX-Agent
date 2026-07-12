from __future__ import annotations

from dataclasses import asdict
from dataclasses import is_dataclass

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.controllers.cgroup_controller import CgroupController


class RollbackSkill:
    name = "rollback"
    description = "Rollback cgroup changes and restore previous state."

    def run(self, context: AgentContext) -> SkillResult:
        cgroup = CgroupController(dry_run=context.dry_run)
        entries = cgroup.rollback()

        context.data["rollback_results"] = entries

        restored_settings = [e for e in entries if is_dataclass(e)]
        removed_groups = [e for e in entries if isinstance(e, dict) and e.get("status") == "removed"]
        skipped = [e for e in entries if isinstance(e, dict) and e.get("status") == "skipped"]

        return SkillResult(
            True,
            f"rollback completed: {len(restored_settings)} settings restored, "
            f"{len(removed_groups)} groups removed, {len(skipped)} skipped",
            {
                "restored": len(restored_settings),
                "groups_removed": len(removed_groups),
                "skipped": len(skipped),
                "entries": [_serialize(e) for e in entries],
            },
        )


def _serialize(value):
    if is_dataclass(value):
        return asdict(value)
    return value
