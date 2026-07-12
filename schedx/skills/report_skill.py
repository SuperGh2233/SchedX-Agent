from __future__ import annotations

from pathlib import Path

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.report.report_generator import ReportGenerator


class ReportSkill:
    name = "report"
    description = "Generate optimization report from execution results."

    def run(self, context: AgentContext) -> SkillResult:
        execution_results = context.data.get("execution_results", [])
        verification = context.data.get("verification", {})

        if not execution_results and not verification:
            return SkillResult(False, "no data to generate report; run act/verify skills first")

        report_data = {
            "context": {
                "dry_run": context.dry_run,
                "mode": context.data.get("mode", "unknown"),
                "target": context.data.get("target", "unknown"),
            },
            "execution_results": execution_results,
            "verification": verification,
            "summary": self._build_summary(execution_results, verification),
        }

        context.data["report"] = report_data
        return SkillResult(
            True,
            "report generated successfully",
            report_data,
        )

    def _build_summary(self, execution_results: list[dict], verification: dict) -> dict:
        exec_summary = verification.get("execution_summary", {})
        recommendations = verification.get("recommendations", [])

        return {
            "total_actions": exec_summary.get("total", 0),
            "successful_actions": exec_summary.get("success", 0) + exec_summary.get("dry_run", 0),
            "failed_actions": exec_summary.get("failed", 0),
            "skipped_actions": exec_summary.get("skipped", 0),
            "success_rate": exec_summary.get("success_rate", 0.0),
            "recommendations": recommendations,
            "status": "success" if exec_summary.get("success_rate", 0.0) >= 0.8 else "needs_attention",
        }
