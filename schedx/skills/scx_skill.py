"""SCX Skill - Integrates scx scheduling policies into the agent pipeline.

This skill applies workload classifications to the scx_agent BPF scheduler
when sched_ext is available on the system.
"""

from __future__ import annotations

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.controllers.scx_controller import ScxController
from schedx.policies.scx_mapper import ScxPolicyMapper
from schedx.scx_daemon import ScxDaemonClient


class ScxSkill:
    """Skill for applying scx scheduling policies.

    This skill checks if sched_ext is available and applies workload
    classifications to the BPF scheduler.
    """

    name = "scx"
    description = "Apply workload policies to scx BPF scheduler."

    def __init__(self) -> None:
        # This skill is the execution path, so use the native scheduler when
        # sched_ext is available. Callers that only need previews can still
        # construct ScxController() directly with its safe dry-run default.
        self.controller = ScxController(dry_run=False)
        self.mapper = ScxPolicyMapper(self.controller)

    def run(self, context: AgentContext) -> SkillResult:
        """Execute the scx skill.

        Args:
            context: Agent context with classification data

        Returns:
            SkillResult with execution status
        """
        # Check if sched_ext is available
        if not self.controller.is_available():
            context.data["scx_status"] = "unavailable"
            return SkillResult(
                True,
                "sched_ext not available; skipping scx policy application",
                {"status": "unavailable", "fallback": "cgroup-only"},
            )

        # Get classification from context
        classification = context.data.get("classification", {})
        if not classification:
            return SkillResult(
                False,
                "no classification data available; run analyze skill first",
            )

        # Get optimization mode
        mode = context.data.get("mode", "latency_first")
        actions = self.mapper.map_classification(classification, mode)

        if context.dry_run:
            return SkillResult(
                True,
                f"previewed {len(actions)} scx policies",
                {"status": "dry_run", "actions": actions},
            )

        daemon = ScxDaemonClient()
        if daemon.is_available():
            results = []
            for action in actions:
                try:
                    success = daemon.set_task_policy(
                        action["pid"], action["class_id"], action["weight"]
                    )
                    results.append({**action, "success": success})
                except Exception as exc:
                    results.append({**action, "success": False, "error": str(exc)})
            summary = {
                "mode": mode,
                "source": "persistent_scx_daemon",
                "total_actions": len(results),
                "successful": sum(item["success"] for item in results),
                "failed": sum(not item["success"] for item in results),
                "results": results,
            }
            context.data["scx_results"] = summary
            context.data["scx_status"] = "active"
            return SkillResult(
                True,
                f"applied {summary['successful']} scx policies through daemon",
                summary,
            )

        # Start scheduler if not running
        if not self.controller.status().get("process_running"):
            try:
                self.controller.start_scheduler("scx_agent")
            except Exception as e:
                context.data["scx_status"] = "error"
                return SkillResult(
                    False,
                    f"failed to start scx scheduler: {e}",
                    {"error": str(e)},
                )

        # Apply policies
        try:
            results = self.mapper.apply_policies(classification, mode)
            context.data["scx_results"] = results
            context.data["scx_status"] = "active"

            return SkillResult(
                True,
                f"applied {results['successful']} scx policies ({results['failed']} failed)",
                results,
            )

        except Exception as e:
            context.data["scx_status"] = "error"
            return SkillResult(
                False,
                f"failed to apply scx policies: {e}",
                {"error": str(e)},
            )


class ScxStatsSkill:
    """Skill for retrieving scx scheduler statistics."""

    name = "scx_stats"
    description = "Retrieve scx BPF scheduler dispatch statistics."

    def __init__(self) -> None:
        self.controller = ScxController()

    def run(self, context: AgentContext) -> SkillResult:
        """Execute the scx stats skill.

        Returns:
            SkillResult with dispatch statistics
        """
        if not self.controller.is_available():
            return SkillResult(
                True,
                "sched_ext not available; no statistics to collect",
                {"status": "unavailable"},
            )

        try:
            stats = self.controller.get_stats()
            policies = self.controller.dump_policies()

            result = {
                "stats": stats.to_dict(),
                "policies": policies,
                "scheduler_status": self.controller.status(),
            }

            context.data["scx_stats"] = result

            return SkillResult(
                True,
                f"collected scx statistics: {stats.total} total dispatches",
                result,
            )

        except Exception as e:
            return SkillResult(
                False,
                f"failed to collect scx statistics: {e}",
                {"error": str(e)},
            )
