from __future__ import annotations

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.llm.client import LLMClient, LLMError


class LlmAnalyzeSkill:
    """Use LLM to provide intelligent workload analysis and strategy recommendations."""

    name = "llm_analyze"
    description = "LLM-powered workload analysis and scheduling strategy recommendation."

    def run(self, context: AgentContext) -> SkillResult:
        client = LLMClient()
        if not client.is_configured():
            return SkillResult(
                False,
                "LLM not configured. Set SCHEDX_LLM_API_KEY environment variable.",
                {"configured": False},
            )

        classification = context.data.get("classification", {})
        snapshot = context.data.get("snapshot", {})
        pressure = snapshot.get("pressure", {})
        topology = context.data.get("topology", {})

        if not classification:
            return SkillResult(False, "No classification data. Run analyze skill first.")

        try:
            analysis = client.analyze_workload(classification, pressure, topology)
        except LLMError as exc:
            return SkillResult(False, str(exc), {"configured": True})
        context.data["llm_analysis"] = analysis

        return SkillResult(
            True,
            "LLM analysis completed",
            {"analysis": analysis},
        )
