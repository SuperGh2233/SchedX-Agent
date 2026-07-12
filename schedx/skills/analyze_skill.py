from __future__ import annotations

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.policies.classifier import WorkloadClassifier


class AnalyzeSkill:
    name = "analyze"
    description = "Classify workloads from a process snapshot."

    def run(self, context: AgentContext) -> SkillResult:
        snapshot = context.data.get("snapshot", {})
        result = WorkloadClassifier().classify_snapshot(snapshot)
        context.data["classification"] = result
        return SkillResult(True, "classification completed", result)

