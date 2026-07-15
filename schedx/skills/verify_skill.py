from __future__ import annotations

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.probes.cgroup_probe import CgroupProbe
from schedx.probes.procfs_probe import ProcfsProbe
from schedx.policies.repository import PolicyRepository
from schedx.policies.verifier import CanaryVerifier


class VerifySkill:
    name = "verify"
    description = "Verify system state after optimization execution."

    def run(self, context: AgentContext) -> SkillResult:
        execution_results = context.data.get("execution_results", [])
        if not execution_results:
            if context.data.get("execution_noop"):
                verification = {
                    "execution_summary": self._summarize_execution([]),
                    "system_state": self._check_system_state(),
                    "recommendations": ["No matching workload was present; no controls were changed."],
                }
                context.data["verification"] = verification
                return SkillResult(True, "verification completed: safe no-op", verification)
            return SkillResult(False, "no execution results to verify; run act skill first")

        verification = {
            "execution_summary": self._summarize_execution(execution_results),
            "system_state": self._check_system_state(),
            "recommendations": [],
        }

        failed_actions = [r for r in execution_results if r.get("status") == "failed_rolled_back"]
        skipped_actions = [r for r in execution_results if r.get("status") == "skipped"]

        if failed_actions:
            verification["recommendations"].append(
                f"Review {len(failed_actions)} failed actions for root cause"
            )

        if skipped_actions:
            verification["recommendations"].append(
                f"{len(skipped_actions)} actions were skipped (protected processes)"
            )

        canary = context.data.get("canary")
        if isinstance(canary, dict):
            verdict = CanaryVerifier().evaluate(
                str(context.data.get("mode", "balanced")),
                canary.get("baseline", {}),
                canary.get("candidate", {}),
                nr_rejected=int(canary.get("nr_rejected", 0) or 0),
                background_share=canary.get("background_share"),
            )
            verdict_data = verdict.to_dict()
            context.data["canary_verdict"] = verdict_data
            verification["canary"] = verdict_data
            route = context.data.get("policy_route", {})
            expert_id = route.get("expert_id") if isinstance(route, dict) else None
            if expert_id:
                PolicyRepository(
                    context.state_dir / "policy_repository.json"
                ).record_outcome(
                    str(expert_id),
                    accepted=verdict.accepted,
                    metrics=verdict.deltas,
                    reason=",".join(verdict.reasons) or verdict.status,
                )
            if not verdict.accepted:
                context.data["rollback_required"] = True
                verification["recommendations"].append(
                    "Canary policy regressed or violated safety constraints; rollback required."
                )
                context.data["verification"] = verification
                return SkillResult(False, "canary rejected; rollback required", verification)
            context.data.pop("rollback_required", None)

        context.data["verification"] = verification
        return SkillResult(
            True,
            f"verification completed: {verification['execution_summary']['success_rate']:.1%} success rate",
            verification,
        )

    def _summarize_execution(self, results: list[dict]) -> dict:
        total = len(results)
        success = sum(1 for r in results if r.get("status") == "ok")
        dry_run = sum(1 for r in results if r.get("status") == "dry_run")
        failed = sum(1 for r in results if r.get("status") == "failed_rolled_back")
        skipped = sum(1 for r in results if r.get("status") == "skipped")

        return {
            "total": total,
            "success": success,
            "dry_run": dry_run,
            "failed": failed,
            "skipped": skipped,
            "success_rate": (success + dry_run) / total if total > 0 else 0.0,
        }

    def _check_system_state(self) -> dict:
        cgroup_probe = CgroupProbe()
        procfs_probe = ProcfsProbe()

        return {
            "cgroup_v2": cgroup_probe.is_v2(),
            "cgroup_controllers": cgroup_probe.controllers(),
            "process_count": len(procfs_probe.snapshot(interval=0.1, top=5).get("processes", [])),
        }
