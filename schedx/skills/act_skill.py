from __future__ import annotations

from schedx.agent.context import AgentContext
from schedx.agent.executor import SafeActionExecutor
from schedx.agent.skill import SkillResult
from schedx.controllers.cgroup_controller import CgroupController


class ActSkill:
    name = "act"
    description = "Execute planned actions with rollback support."

    def run(self, context: AgentContext) -> SkillResult:
        actions = context.data.get("actions", [])
        if not actions:
            context.data["execution_results"] = []
            context.data["execution_noop"] = True
            return SkillResult(True, "no matching actions; execution completed as a safe no-op")

        context.data.pop("execution_noop", None)
        cgroup = CgroupController(dry_run=context.dry_run, rollback_file=context.rollback_file,
                                  owner=context.session.session_id if context.session else None,
                                  transaction=context.data.get("transaction_id"))
        context.data["transaction_owner"] = cgroup.owner
        executor = SafeActionExecutor(cgroup, scope_pids=context.data.get("scope_pids"))
        results = executor.execute(actions, dry_run=context.dry_run)

        context.data["execution_results"] = results

        failed = [r for r in results if r.get("status") in ("failed", "failed_rolled_back", "rollback_failed", "unsupported")]
        if failed:
            return SkillResult(
                False,
                f"execution completed with {len(failed)} failures",
                {"results": results, "failures": len(failed)},
            )

        return SkillResult(
            True,
            f"executed {len(results)} actions successfully",
            {"results": results},
        )
