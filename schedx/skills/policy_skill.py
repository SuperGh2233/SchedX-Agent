from __future__ import annotations

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.policies.planner import PolicyPlanner
from schedx.probes.cpu_topology_probe import CpuTopologyProbe


class PolicySkill:
    name = "policy"
    description = "Generate action plan based on workload classification and optimization mode."

    def __init__(self) -> None:
        self.planner = PolicyPlanner()

    def run(self, context: AgentContext) -> SkillResult:
        classification = context.data.get("classification", {})
        mode = context.data.get("mode", "latency_first")
        target = context.data.get("target", "")
        parameters = context.data.get("agent_decision", {}).get("parameters", {})

        if not classification:
            return SkillResult(False, "no classification data available; run analyze skill first")

        topology = CpuTopologyProbe().get_topology()
        context.data["topology"] = topology

        actions = self.planner.plan(mode, target, classification, topology, parameters)
        context.data["actions"] = actions

        return SkillResult(
            True,
            f"planned {len(actions)} actions for mode={mode}",
            {"actions": [str(a) for a in actions], "count": len(actions)},
        )
