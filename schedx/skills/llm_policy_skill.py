from __future__ import annotations

from dataclasses import asdict

from schedx.agent.context import AgentContext
from schedx.agent.decision import DecisionEngine
from schedx.agent.skill import SkillResult
from schedx.controllers.scx_controller import ScxController
from schedx.llm.client import LLMError
from schedx.llm.policy_planner import LLMPolicyPlanner


class LlmPolicySkill:
    name = "llm_policy"
    description = "Generate and validate a constrained DeepSeek scheduling policy."

    def run(self, context: AgentContext) -> SkillResult:
        classification = context.data.get("classification", {})
        pressure = context.data.get("snapshot", {}).get("pressure", {})
        topology = context.data.get("topology", {})
        fallback = DecisionEngine().decide(
            classification, pressure, topology, context.data.get("target", "")
        )
        try:
            decision = LLMPolicyPlanner().propose(
                classification, pressure, topology, fallback, ScxController().status()
            )
        except LLMError as exc:
            decision = fallback
            source = "rule_fallback"
            error = str(exc)
        else:
            source = "deepseek-v4"
            error = ""

        context.data["mode"] = decision.mode
        context.data["target"] = decision.target
        context.data["agent_decision"] = {**asdict(decision), "source": source}
        return SkillResult(
            True,
            f"policy selected by {source}",
            {"decision": context.data["agent_decision"], "llm_error": error},
        )
