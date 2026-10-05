from __future__ import annotations

from schedx.agent.context import AgentContext
from schedx.agent.skill import SkillResult
from schedx.probes.slo_probe import (
    WrkCanaryConfig,
    WrkSloProbe,
    background_pids,
    sched_ext_rejected,
)


class CanaryBaselineSkill:
    name = "canary_baseline"
    description = "Measure the service SLO before applying a candidate policy."

    def run(self, context: AgentContext) -> SkillResult:
        raw_config = context.data.get("canary_config")
        if not isinstance(raw_config, dict):
            return SkillResult(True, "SLO canary disabled", {"status": "disabled"})
        if context.dry_run:
            return SkillResult(True, "SLO canary skipped in dry-run", {"status": "dry_run"})

        try:
            config = WrkCanaryConfig(**raw_config)
            probe = context.data.get("_slo_probe")
            if not isinstance(probe, WrkSloProbe):
                probe = WrkSloProbe()
                context.data["_slo_probe"] = probe
            pids = background_pids(context.data.get("classification", {}))
            baseline = probe.sample(config, pids)
            context.data["canary"] = {
                "baseline": baseline,
                "background_expected": bool(pids),
            }
            return SkillResult(True, "captured baseline SLO canary", baseline)
        except (RuntimeError, ValueError) as exc:
            return SkillResult(False, f"baseline SLO canary failed: {exc}")


class CanaryCandidateSkill:
    name = "canary_candidate"
    description = "Measure the service SLO after applying a candidate policy."

    def run(self, context: AgentContext) -> SkillResult:
        raw_config = context.data.get("canary_config")
        if not isinstance(raw_config, dict):
            return SkillResult(True, "SLO canary disabled", {"status": "disabled"})
        if context.dry_run:
            return SkillResult(True, "SLO canary skipped in dry-run", {"status": "dry_run"})

        canary = context.data.get("canary")
        if not isinstance(canary, dict) or not isinstance(canary.get("baseline"), dict):
            return SkillResult(False, "baseline SLO canary is missing")
        try:
            config = WrkCanaryConfig(**raw_config)
            probe = context.data.get("_slo_probe")
            if not isinstance(probe, WrkSloProbe):
                return SkillResult(False, "SLO probe state is missing")
            pids = background_pids(context.data.get("classification", {}))
            candidate = probe.sample(config, pids)
            baseline_ticks = int(canary["baseline"].get("background_cpu_ticks", 0) or 0)
            candidate_ticks = int(candidate.get("background_cpu_ticks", 0) or 0)
            retention = candidate_ticks / baseline_ticks if baseline_ticks else None
            canary.update(
                {
                    "candidate": candidate,
                    "background_share": candidate.get("background_cpu_share") if pids else None,
                    "background_retention": retention,
                    "nr_rejected": sched_ext_rejected(),
                }
            )
            return SkillResult(
                True,
                "captured candidate SLO canary",
                {
                    "candidate": candidate,
                    "background_retention": retention,
                    "nr_rejected": canary["nr_rejected"],
                },
            )
        except (RuntimeError, ValueError) as exc:
            return SkillResult(False, f"candidate SLO canary failed: {exc}")
