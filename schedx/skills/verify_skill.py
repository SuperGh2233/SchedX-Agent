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

        failed_actions = [r for r in execution_results if r.get("status") in {"failed", "failed_rolled_back", "rollback_failed", "unsupported"}]
        skipped_actions = [r for r in execution_results if r.get("status") == "skipped"]

        if failed_actions:
            context.data["rollback_required"] = True
            context.data["verification"] = verification
            return SkillResult(False, "execution failed; rollback required", verification)

        if skipped_actions:
            verification["recommendations"].append(
                f"{len(skipped_actions)} actions were skipped (protected processes)"
            )

        canary = context.data.get("canary")
        if isinstance(canary, dict):
            try:
                verifier = CanaryVerifier(
                    min_background_retention=context.data.get("canary_min_background_retention", 0.25),
                    min_p99_improvement_percent=context.data.get(
                        "canary_min_p99_improvement"
                    ),
                    **context.data.get("canary_error_limits", {}),
                )
            except (TypeError, ValueError) as exc:
                verdict_data = {
                    "accepted": False,
                    "status": "invalid_config",
                    "reasons": [str(exc)],
                    "deltas": {},
                }
                context.data["canary_verdict"] = verdict_data
                context.data["rollback_required"] = True
                verification["canary"] = verdict_data
                verification["recommendations"].append(
                    "Canary configuration is invalid; rollback required."
                )
                context.data["verification"] = verification
                return SkillResult(False, "invalid canary configuration; rollback required", verification)

            verdict = verifier.evaluate(
                str(context.data.get("mode", "balanced")),
                canary.get("baseline", {}),
                canary.get("candidate", {}),
                nr_rejected=canary.get("nr_rejected", 0),
                background_share=canary.get("background_share"),
                background_retention=canary.get("background_retention"),
                background_expected=bool(canary.get("background_expected")),
                error_metrics_expected=isinstance(context.data.get("canary_config"), dict),
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
                    accepted=(
                        verdict.accepted if verdict.status != "inconclusive" else None
                    ),
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
        failed = sum(1 for r in results if r.get("status") in {"failed", "failed_rolled_back", "rollback_failed", "unsupported"})
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
