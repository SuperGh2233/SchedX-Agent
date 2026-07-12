from __future__ import annotations

import json
from pathlib import Path

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.llm.client import LLMClient, LLMError


class LlmReportSkill:
    """Use LLM to generate natural language experiment reports."""

    name = "llm_report"
    description = "LLM-powered natural language report generation."

    def run(self, context: AgentContext) -> SkillResult:
        client = LLMClient()
        if not client.is_configured():
            return SkillResult(
                False,
                "LLM not configured. Set SCHEDX_LLM_API_KEY environment variable.",
                {"configured": False},
            )

        results_path = Path(context.data.get("results_path", "results"))
        experiment_data = self._load_results(results_path)

        if not experiment_data:
            return SkillResult(False, "No experiment data found in results directory.")

        try:
            report = client.generate_report(experiment_data)
        except LLMError as exc:
            return SkillResult(False, str(exc), {"configured": True})
        context.data["llm_report"] = report

        return SkillResult(
            True,
            "LLM report generated",
            {"report": report},
        )

    def _load_results(self, results_dir: Path) -> dict:
        data = {}
        if not results_dir.exists():
            return data
        for path in sorted(results_dir.glob("*.json")):
            try:
                data[path.name] = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
        return data
