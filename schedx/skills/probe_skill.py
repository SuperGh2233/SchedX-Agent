from __future__ import annotations

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.probes.procfs_probe import ProcfsProbe


class ProbeSkill:
    name = "probe"
    description = "Collect procfs and pressure stall information."

    def run(self, context: AgentContext) -> SkillResult:
        try:
            pids = context.data.get("scope_pids")
            if pids is not None and not isinstance(pids, list):
                raise ValueError("scope_pids must be a list")
            snapshot = ProcfsProbe().snapshot(pids=pids)
        except ValueError as exc:
            return SkillResult(False, f"invalid probe scope: {exc}")
        context.data["snapshot"] = snapshot
        return SkillResult(True, "probe completed", snapshot)
