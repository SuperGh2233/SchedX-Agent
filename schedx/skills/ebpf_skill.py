"""eBPF Skills - Integrates eBPF hooks into the agent pipeline.

These skills manage eBPF program lifecycle and policy application.
"""

from __future__ import annotations

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.controllers.ebpf_controller import EbpfController, EbpfProgType
from schedx.probes.ebpf_probe import EbpfProbe


class EbpfLoadSkill:
    """Skill for loading eBPF programs.

    This skill loads eBPF programs for scheduling, network, and resource control.
    """

    name = "ebpf_load"
    description = "Load eBPF programs for enhanced resource control."

    def run(self, context: AgentContext) -> SkillResult:
        """Execute the eBPF load skill.

        Args:
            context: Agent context

        Returns:
            SkillResult with load status
        """
        existing = context.data.get("_ebpf_probe")
        if isinstance(existing, EbpfProbe) and context.data.get("ebpf_status") == "attached":
            return SkillResult(True, "reusing active eBPF programs", {"status": "reused"})

        probe = EbpfProbe(dry_run=context.dry_run)
        context.data["_ebpf_probe"] = probe
        if not probe.is_available():
            context.data["ebpf_status"] = "unavailable"
            return SkillResult(
                True,
                "eBPF not available; skipping program loading",
                {"status": "unavailable", "fallback": "procfs-only"},
            )

        try:
            if not context.dry_run and probe.controller.has_pinned_programs():
                cleanup = probe.cleanup_pinned()
                context.data["ebpf_stale_cleanup"] = cleanup
                if not all(cleanup.values()):
                    return SkillResult(False, "failed to clean stale eBPF programs", cleanup)
            results = probe.load_all()
            context.data["ebpf_load_results"] = results

            successful = sum(1 for v in results.values() if v)
            failed = sum(1 for v in results.values() if not v)

            context.data["ebpf_status"] = "loaded" if successful > 0 else "error"

            return SkillResult(
                successful > 0,
                f"loaded {successful} eBPF programs ({failed} failed)",
                {"results": results, "successful": successful, "failed": failed},
            )

        except Exception as e:
            context.data["ebpf_status"] = "error"
            return SkillResult(
                False,
                f"failed to load eBPF programs: {e}",
                {"error": str(e)},
            )


class EbpfAttachSkill:
    """Skill for attaching eBPF programs to hooks.

    This skill attaches loaded eBPF programs to their respective hooks.
    """

    name = "ebpf_attach"
    description = "Attach eBPF programs to kernel hooks."

    def run(self, context: AgentContext) -> SkillResult:
        """Execute the eBPF attach skill.

        Args:
            context: Agent context

        Returns:
            SkillResult with attach status
        """
        ebpf_status = context.data.get("ebpf_status")
        if ebpf_status != "loaded":
            return SkillResult(
                True,
                "eBPF programs not loaded; skipping attach",
                {"status": "skipped"},
            )

        probe = context.data.get("_ebpf_probe")
        if not isinstance(probe, EbpfProbe):
            return SkillResult(False, "eBPF controller state missing; run load skill first")

        try:
            results = probe.attach_all()
            context.data["ebpf_attach_results"] = results

            successful = sum(1 for v in results.values() if v)
            failed = sum(1 for v in results.values() if not v)

            context.data["ebpf_status"] = "attached" if successful > 0 else "error"
            if successful == 0:
                probe.unload_all()
                context.data["ebpf_status"] = "unavailable"

            return SkillResult(
                successful > 0,
                f"attached {successful} eBPF programs ({failed} failed)",
                {"results": results, "successful": successful, "failed": failed},
            )

        except Exception as e:
            context.data["ebpf_status"] = "error"
            return SkillResult(
                False,
                f"failed to attach eBPF programs: {e}",
                {"error": str(e)},
            )


class EbpfPolicySkill:
    """Skill for applying eBPF policies.

    This skill applies workload-specific policies to eBPF programs.
    """

    name = "ebpf_policy"
    description = "Apply workload policies to eBPF programs."

    def run(self, context: AgentContext) -> SkillResult:
        """Execute the eBPF policy skill.

        Args:
            context: Agent context with classification data

        Returns:
            SkillResult with policy application status
        """
        ebpf_status = context.data.get("ebpf_status")
        if ebpf_status not in ("loaded", "attached"):
            return SkillResult(
                True,
                "eBPF programs not active; skipping policy application",
                {"status": "skipped"},
            )

        classification = context.data.get("classification", {})
        if not classification:
            return SkillResult(
                False,
                "no classification data available; run analyze skill first",
            )

        try:
            probe = context.data.get("_ebpf_probe")
            if not isinstance(probe, EbpfProbe):
                return SkillResult(False, "eBPF controller state missing; run load skill first")
            results = self._apply_policies(classification, probe.controller)
            context.data["ebpf_policy_results"] = results

            successful = sum(1 for r in results if r.get("success"))
            failed = sum(1 for r in results if not r.get("success"))

            return SkillResult(
                True,
                f"applied {successful} eBPF policies ({failed} failed)",
                {"results": results, "successful": successful, "failed": failed},
            )

        except Exception as e:
            return SkillResult(
                False,
                f"failed to apply eBPF policies: {e}",
                {"error": str(e)},
            )

    def _apply_policies(self, classification: dict, controller: EbpfController) -> list[dict]:
        """Apply policies based on workload classification."""
        results = []
        groups = classification.get("groups", {})

        for workload_type, processes in groups.items():
            if workload_type == "unknown":
                continue

            class_id = EbpfController.classify_to_ebpf_class(workload_type)

            for proc in processes:
                pid = proc.get("pid")
                if pid is None:
                    continue

                entry = {
                    "pid": pid,
                    "workload_type": workload_type,
                    "class_id": class_id,
                    "sched_policy": "delegated_to_scx_or_cgroup",
                }

                cgroup_id = controller.cgroup_id_for_pid(pid)
                entry["cgroup_id"] = cgroup_id
                if cgroup_id is None:
                    reason = controller.last_error
                    entry.update({
                        "net_policy": "deferred",
                        "resource_policy": "deferred",
                        "security_policy": "deferred",
                        "status": "deferred",
                        "reason": reason,
                        "success": "shared cgroup" in reason,
                    })
                    results.append(entry)
                    continue

                policy_statuses = []
                if controller.is_program_loaded(EbpfProgType.NET_POLICY):
                    ok = controller.update_net_cgroup_policy(cgroup_id, class_id)
                    entry["net_policy"] = "applied" if ok else "failed"
                    policy_statuses.append(ok)
                else:
                    entry["net_policy"] = "skipped"

                if controller.is_program_loaded(EbpfProgType.RESOURCE_CTRL):
                    ok = controller.update_resource_policy(cgroup_id, class_id)
                    entry["resource_policy"] = "applied" if ok else "failed"
                    policy_statuses.append(ok)
                else:
                    entry["resource_policy"] = "skipped"

                if controller.is_program_loaded(EbpfProgType.SECURITY_POLICY):
                    try:
                        executable = controller.proc_root / str(pid) / "exe"
                        if not controller.dry_run:
                            executable = executable.resolve(strict=True)
                        ok = controller.update_security_policy(
                            cgroup_id,
                            executable,
                            deny_exec=False,
                            audit_only=True,
                        )
                    except OSError as exc:
                        controller.last_error = f"cannot resolve executable for pid {pid}: {exc}"
                        ok = False
                    entry["security_policy"] = "audit" if ok else "failed"
                    policy_statuses.append(ok)
                else:
                    entry["security_policy"] = "skipped"

                success = all(policy_statuses)
                entry.update({
                    "status": "applied" if success else "failed",
                    "reason": "" if success else controller.last_error,
                    "success": success,
                })
                results.append(entry)

        return results


class EbpfStatsSkill:
    """Skill for collecting eBPF statistics.

    This skill collects statistics from all loaded eBPF programs.
    """

    name = "ebpf_stats"
    description = "Collect statistics from eBPF programs."

    def run(self, context: AgentContext) -> SkillResult:
        """Execute the eBPF stats skill.

        Args:
            context: Agent context

        Returns:
            SkillResult with statistics
        """
        try:
            probe = context.data.get("_ebpf_probe")
            if not isinstance(probe, EbpfProbe):
                return SkillResult(True, "eBPF is inactive; statistics skipped", {"status": "skipped"})
            snapshot = probe.snapshot()
            context.data["ebpf_stats"] = snapshot

            return SkillResult(
                True,
                "collected eBPF statistics",
                snapshot,
            )

        except Exception as e:
            return SkillResult(
                False,
                f"failed to collect eBPF statistics: {e}",
                {"error": str(e)},
            )


class EbpfCleanupSkill:
    """Unload active hooks; used by rollback and explicit cleanup paths."""

    name = "ebpf_cleanup"
    description = "Detach eBPF links and remove pinned maps/programs."

    def run(self, context: AgentContext) -> SkillResult:
        probe = context.data.get("_ebpf_probe")
        try:
            if isinstance(probe, EbpfProbe):
                results = probe.unload_all()
            else:
                probe = EbpfProbe(dry_run=context.dry_run)
                results = probe.cleanup_pinned()
            ok = all(results.values())
            context.data["ebpf_cleanup"] = results
            if ok:
                context.data["ebpf_status"] = "unloaded"
                context.data.pop("_ebpf_probe", None)
            return SkillResult(
                ok,
                f"eBPF cleanup completed: {sum(results.values())}/{len(results)} removed",
                {"results": results},
            )
        except Exception as exc:
            return SkillResult(False, f"failed to clean eBPF programs: {exc}", {"error": str(exc)})
