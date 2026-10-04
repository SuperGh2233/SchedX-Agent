"""SCX Skill - Integrates scx scheduling policies into the agent pipeline.

This skill applies workload classifications to the scx_agent BPF scheduler
when sched_ext is available on the system.
"""

from __future__ import annotations

import json
from pathlib import Path

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.controllers.scx_controller import ScxController, SCX_FAIRNESS_THROUGHPUT_BACKGROUND
from schedx.policies.scx_mapper import ScxPolicyMapper
from schedx.scx_daemon import ScxDaemonClient
from schedx.state import atomic_json, process_start_time, state_lock


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

        if not actions:
            return SkillResult(True, "no scx policy changes", {"status": "noop"})

        if context.dry_run:
            return SkillResult(
                True,
                f"previewed {len(actions)} scx policies",
                {"status": "dry_run", "actions": actions},
            )

        if context.scx_rollback_file.exists():
            existing = json.loads(context.scx_rollback_file.read_text())
            owner = context.session.session_id if context.session else None
            if existing.get("owner") not in (None, owner):
                context.data["scx_status"] = "error"
                return SkillResult(False, "existing scheduling transaction requires explicit rollback")

        daemon = ScxDaemonClient()
        if daemon.is_available():
            previous = daemon.request("policies").get("task_policies", {})
            actions = [row for row in actions if previous.get(row["pid"], previous.get(str(row["pid"]))) != {"class_id": row["class_id"], "weight": row["weight"]}]
            if not actions:
                return SkillResult(True, "scx policies already match", {"status": "noop"})
            self._record_rollback(context, "persistent_scx_daemon", actions, previous)
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
            context.data["scx_status"] = "active" if not summary["failed"] else "error"
            return SkillResult(
                not summary["failed"],
                f"applied {summary['successful']} scx policies through daemon",
                summary,
            )

        # Start scheduler if not running
        if not self.controller.status().get("process_running"):
            try:
                # Persist lifecycle ownership before attachment, including a
                # failed start that cannot subsequently stop its child.
                context.data["_scx_controller"] = self.controller
                self._record_rollback(context, "standalone_scx", [], {})
                if mode == "throughput_first":
                    self.controller.background_interval = SCX_FAIRNESS_THROUGHPUT_BACKGROUND
                if not self.controller.start_scheduler("scx_agent"):
                    raise RuntimeError("scheduler did not start")
            except Exception as e:
                context.data["scx_status"] = "error"
                return SkillResult(
                    False,
                    f"failed to start scx scheduler: {e}",
                    {"error": str(e)},
                )

        # Apply policies
        try:
            self._record_rollback(context, "standalone_scx", actions, self.controller.dump_policies().get("task_policies", {}))
            context.data["_scx_controller"] = self.controller
            results = self.mapper.apply_policies(classification, mode)
            context.data["scx_results"] = results
            context.data["scx_status"] = "active" if not results["failed"] else "error"

            return SkillResult(
                not results["failed"],
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

    @staticmethod
    def _record_rollback(context: AgentContext, source: str, actions: list[dict], previous: dict | None = None, scope: str = "task") -> None:
        owner = context.session.session_id if context.session else None
        context.data["transaction_owner"] = owner
        with state_lock(context.scx_rollback_file):
            payload = json.loads(context.scx_rollback_file.read_text()) if context.scx_rollback_file.exists() else {"source": source, "owner": owner, "entries": []}
            if payload.get("owner") not in (None, owner):
                raise RuntimeError("scheduler policy transaction is owned by another operation")
            entries = payload.setdefault("entries", [{"pid": int(pid), "previous": None} for pid in payload.get("pids", [])])
            transaction = context.data.get("transaction_id")
            id_field = "pid" if scope == "task" else "cgroup_id"
            recorded = {row.get(id_field) for row in entries if row.get("scope", "task") == scope and row.get("transaction") == transaction}
            previous = previous or {}
            for action in actions:
                pid = int(action[id_field])
                if pid not in recorded:
                    entries.append({id_field: pid, "scope": scope, "pid_start": process_start_time(pid) if scope == "task" else None, "previous": previous.get(pid, previous.get(str(pid))), "transaction": transaction})
                    recorded.add(pid)
            payload.update(source=source, owner=owner, pids=sorted({row["pid"] for row in entries if "pid" in row}))
            atomic_json(context.scx_rollback_file, payload)
            context.data["scx_rollback"] = payload


class ScxCgroupSkill:
    """Mirror policies after migration so worker threads and children inherit them."""
    name = "scx_cgroups"

    def run(self, context: AgentContext) -> SkillResult:
        if context.dry_run or context.data.get("scx_status") != "active":
            return SkillResult(True, "cgroup scheduling mirror skipped")
        daemon = ScxDaemonClient()
        source = "persistent_scx_daemon" if daemon.is_available() else "standalone_scx"
        client = daemon if source == "persistent_scx_daemon" else context.data.get("_scx_controller")
        if client is None:
            return SkillResult(False, "active scheduler controller is unavailable")
        previous = (client.request("policies") if source == "persistent_scx_daemon" else client.dump_policies()).get("cgroup_policies", {})
        desired = {row["pid"]: row for row in ScxPolicyMapper().map_classification(context.data.get("classification", {}), context.data.get("mode", "balanced"))}
        actions = {}
        for result in context.data.get("execution_results", []):
            pid = result.get("pid")
            if result.get("status") != "ok" or not result.get("group") or pid not in desired:
                continue
            path = Path(context.data.get("cgroup_root", "/sys/fs/cgroup")) / "schedx" / result["group"]
            if not path.exists():
                return SkillResult(False, "managed cgroup disappeared before scheduling registration")
            cgroup_id = path.stat().st_ino
            row = desired[pid]
            value = {"class_id": row["class_id"], "weight": row["weight"]}
            if previous.get(cgroup_id, previous.get(str(cgroup_id))) != value:
                actions[cgroup_id] = {"cgroup_id": cgroup_id, **value}
        if actions:
            ScxSkill._record_rollback(context, source, list(actions.values()), previous, "cgroup")
        failures = 0
        for row in actions.values():
            if not client.set_cgroup_policy(row["cgroup_id"], row["class_id"], row["weight"]):
                failures += 1
        if source == "standalone_scx" and failures == 0:
            client.enable_adaptive_fairness()
        return SkillResult(failures == 0, f"mirrored {len(actions) - failures} cgroup policies", {"failed": failures})


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
