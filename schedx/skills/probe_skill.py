from __future__ import annotations

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.probes.procfs_probe import ProcfsProbe


class ProbeSkill:
    name = "probe"
    description = "Collect procfs and pressure stall information."

    def run(self, context: AgentContext) -> SkillResult:
        snapshot = ProcfsProbe().snapshot()
        context.data["snapshot"] = snapshot
        return SkillResult(True, "probe completed", snapshot)

