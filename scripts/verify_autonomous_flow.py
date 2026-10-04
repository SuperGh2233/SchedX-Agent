#!/usr/bin/env python3
"""Run the real Agent phases on owned changing loads and an HTTP fault.

The hooks below change the workload and save evidence; _run_one_round, probes,
classification, routing, actions, Canary and rollback use production code.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schedx.agent.context import AgentContext
from schedx.agent.decision import DecisionEngine
from schedx.agent.loop import AgentLoop
from schedx.agent.skill import SkillResult
from schedx.benchmark.scoped_workloads import OwnedWorkloads
from schedx.benchmark.versioning import version_identity
from schedx.controllers.scx_controller import ScxController
from schedx.policies.verifier import CanaryVerifier
from schedx.scx_daemon import ScxDaemonClient
from schedx.skills.rollback_skill import RollbackSkill
from schedx.state import atomic_json


def scenario_phase(round_number: int, rounds_per_stage: int) -> str:
    if round_number <= rounds_per_stage:
        return "stable"
    if round_number <= rounds_per_stage * 2:
        return "interference"
    if round_number <= rounds_per_stage * 3:
        return "recovery"
    return "http_fault"


def task_state(pid: int) -> dict:
    return {
        "cgroup": Path(f"/proc/{pid}/cgroup").read_text(),
        "nice": os.getpriority(os.PRIO_PROCESS, pid),
        "affinity": sorted(os.sched_getaffinity(pid)),
        "policy": os.sched_getscheduler(pid),
        "priority": os.sched_getparam(pid).sched_priority,
    }


def safe_json(value):
    if is_dataclass(value):
        return safe_json(asdict(value))
    if isinstance(value, dict):
        return {str(key): safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_json(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


class ScenarioLoop(AgentLoop):
    def __init__(self, context, workloads, output, rounds_per_stage):
        super().__init__(context)
        self.workloads = workloads
        self.output = output
        self.rounds_per_stage = rounds_per_stage
        self.original_tasks = {}
        self.evidence = {}
        self.phase_results = {}

    def _execute_skill(self, phase, iteration):
        index = self.context.data["round"]
        stage = scenario_phase(index, self.rounds_per_stage)
        if phase == "probe":
            self.workloads.noise(stage == "interference")
            scope = self.workloads.pids()
            self.context.data["scope_pids"] = scope
            for pid in scope:
                if pid not in self.original_tasks:
                    self.original_tasks[pid] = task_state(pid)
        fault = stage == "http_fault" and phase == "canary_candidate"
        if fault:
            self.workloads.error_flag.touch()
        try:
            result = super()._execute_skill(phase, iteration)
            self.phase_results.setdefault(index, {})[phase] = safe_json(result)
            return result
        finally:
            if fault:
                self.workloads.error_flag.unlink(missing_ok=True)

    def _save_continuous_state(self, rounds):
        super()._save_continuous_state(rounds)
        if not self.round_history:
            return
        row = self.round_history[-1]
        evidence = {"stage": scenario_phase(row["round"], self.rounds_per_stage), "result": copy.deepcopy(row)}
        for key in ("snapshot", "classification", "agent_decision", "policy_route", "actions",
                    "execution_results", "scx_results", "ebpf_status", "ebpf_policy_results",
                    "canary", "canary_verdict", "rollback", "degraded_phases", "stability", "converged"):
            if key in self.context.data:
                evidence[key] = safe_json(self.context.data[key])
        evidence["scheduler_state"] = ScxController().state()
        evidence["phase_results"] = self.phase_results.get(row["round"], {})
        self.evidence[row["round"]] = evidence
        atomic_json(self.output / f"round-{row['round']:03d}.json", evidence)


def assess(evidence: dict[int, dict], rounds_per_stage: int) -> dict:
    failures = []
    expected_count = rounds_per_stage * 3 + 1
    if len(evidence) != expected_count:
        failures.append("incomplete_rounds")
    stages = {stage: [row for row in evidence.values() if row["stage"] == stage]
              for stage in ("stable", "interference", "recovery", "http_fault")}
    for stage in ("stable", "interference", "recovery"):
        if not any(row["result"].get("objective_status") == "accepted" for row in stages[stage]):
            failures.append(f"{stage}_has_no_accepted_measurement")
        for row in stages[stage]:
            result = row["result"]
            expected_rejection = result.get("status") == "failed_rolled_back" and result.get("failed_phase") == "verify"
            if result.get("status") != "ok" and not expected_rejection:
                failures.append(f"unexpected_failure_round_{result['round']}")
            canary = row.get("canary", {})
            measured = CanaryVerifier().evaluate(
                str(row.get("agent_decision", {}).get("mode", "balanced")),
                canary.get("baseline", {}), canary.get("candidate", {}),
                nr_rejected=canary.get("nr_rejected", 0),
                background_share=canary.get("background_share"),
                background_retention=canary.get("background_retention"),
                background_expected=bool(canary.get("background_expected")), error_metrics_expected=True,
            )
            if measured.status != row.get("canary_verdict", {}).get("status") or measured.status == "inconclusive":
                failures.append(f"missing_or_inconsistent_measurement_round_{result['round']}")
            if row.get("degraded_phases") or row.get("ebpf_status") != "attached":
                failures.append(f"incomplete_ebpf_chain_round_{result['round']}")
            phases = row.get("phase_results", {})
            required = set(AgentLoop.PHASES) - {"decide"}
            if not required.issubset(phases):
                failures.append(f"missing_production_phase_round_{result['round']}")
            if any(phases.get(phase, {}).get("data", {}).get("failed", 0) for phase in ("ebpf_load", "ebpf_attach")):
                failures.append(f"partial_ebpf_backend_round_{result['round']}")
            if row.get("scheduler_state") != "enabled":
                failures.append(f"scheduler_inactive_round_{result['round']}")
            if not any(action.get("status") == "ok" for action in row.get("execution_results", [])):
                failures.append(f"no_successful_actuation_round_{result['round']}")
            if not row.get("scx_results", {}).get("successful"):
                failures.append(f"no_acknowledged_native_policy_round_{result['round']}")
            if not any(policy.get("success") for policy in row.get("ebpf_policy_results", [])):
                failures.append(f"no_targeted_ebpf_policy_round_{result['round']}")
            scoped = set(row.get("snapshot", {}).get("scope_pids", []))
            planned = {int(action["target"]) for action in row.get("actions", []) if action.get("target_type") == "pid"}
            if not planned.issubset(scoped):
                failures.append(f"out_of_scope_action_round_{result['round']}")
    for stage in ("stable", "recovery"):
        if not any(row.get("converged") for row in stages[stage]):
            failures.append(f"{stage}_plateau_not_observed")
    engine = DecisionEngine(stability_window=3, stability_tolerance_percent=10.0)
    history = []
    for row in sorted(evidence.values(), key=lambda item: item["result"]["round"]):
        history.append(row["result"])
        if row.get("converged") and not engine.assess_stability(history)["stable"]:
            failures.append(f"convergence_not_supported_by_metrics_round_{row['result']['round']}")
    if not any(row.get("classification", {}).get("groups", {}).get("background_noise") for row in stages["interference"]):
        failures.append("interference_not_observed")
    if any(row.get("classification", {}).get("groups", {}).get("background_noise") for row in stages["recovery"]):
        failures.append("removed_interference_still_classified")
    fault = stages["http_fault"][-1] if stages["http_fault"] else {}
    result = fault.get("result", {})
    verdict = fault.get("canary_verdict", {})
    response_errors = fault.get("canary", {}).get("candidate", {}).get("non_success_responses", 0)
    if not (result.get("status") == "failed_rolled_back" and result.get("failed_phase") == "verify"
            and result.get("rollback_success") and response_errors > 0
            and "response_errors" in verdict.get("reasons", [])):
        failures.append("http_error_not_measured_rejected_and_recovered")
    fault_phases = fault.get("phase_results", {})
    if (not ((set(AgentLoop.PHASES) - {"decide"}) | {"rollback"}).issubset(fault_phases)
            or fault_phases.get("verify", {}).get("ok") is not False
            or fault_phases.get("rollback", {}).get("ok") is not True):
        failures.append("fault_recovery_phase_evidence_missing")
    return {"status": "passed" if not failures else "failed", "failures": failures,
            "completed_rounds": len(evidence), "fault_response_errors": response_errors,
            "claim_scope": "real Agent flow, changing-load observation and controlled HTTP fault recovery; no performance gain is implied"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--candidate-ref", required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--rounds-per-stage", type=int, default=5)
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        parser.error("real Agent validation requires Linux root; run local tests on other platforms")
    if args.rounds_per_stage < 3 or args.duration < 1 or args.workers < 1:
        parser.error("at least three rounds per stage and positive duration/workers are required")
    if ScxController().state() != "disabled" or ScxDaemonClient().is_available():
        parser.error("validation requires no active native scheduler or persistent daemon")
    identity = version_identity(args.repository, Path(__file__).resolve().parents[1], args.candidate_ref,
                                args.binary, args.build_manifest)
    btf_digest = hashlib.sha256(Path("/sys/kernel/btf/vmlinux").read_bytes()).hexdigest()
    if (identity["build_manifest"].get("kernel_release") != os.uname().release
            or identity["build_manifest"].get("btf_sha256") != btf_digest):
        parser.error("build manifest must match the running kernel and BTF")
    args.output.mkdir(parents=True, exist_ok=False)
    binary_dir = args.output.resolve() / "bin"
    binary_dir.mkdir()
    (binary_dir / "scx_agent").symlink_to(args.binary.resolve())
    old_path = os.environ["PATH"]
    os.environ["PATH"] = str(binary_dir) + os.pathsep + old_path
    context = AgentContext(dry_run=False, state_dir=args.output / "state", data={
        "canary_config": {"url": "", "duration": args.duration, "connections": 32, "threads": 2},
        "canary_min_background_retention": 0.25,
        "stability_window": 3, "stability_tolerance_percent": 10.0,
    })
    context.data["transaction_owner"] = context.session.session_id
    workloads = OwnedWorkloads(args.output / "workloads", workers=args.workers)
    summary = {"status": "running", "version_identity": identity, "kernel": os.uname().release,
               "scope": "owned services and PIDs", "stability_tolerance_percent": 10.0}
    loop = None
    try:
        workloads.__enter__()
        context.data["canary_config"]["url"] = workloads.url
        loop = ScenarioLoop(context, workloads, args.output, args.rounds_per_stage)
        summary["stop_reason"] = loop.run_continuous(interval=0, max_rounds=args.rounds_per_stage * 3 + 1)
        summary["assessment"] = assess(loop.evidence, args.rounds_per_stage)
        summary["status"] = summary["assessment"]["status"]
    except BaseException as exc:
        summary.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        # Restore the whole owned session, rather than just the last candidate.
        context.data.pop("transaction_id", None)
        context.data.pop("accepted_classification", None)
        context.data.pop("accepted_cgroup_ids", None)
        probe = context.data.get("_ebpf_probe")
        rollback = RollbackSkill().run(context) if loop is not None else SkillResult(True, "no Agent mutation started")
        summary["rollback"] = safe_json(rollback)
        if loop is not None:
            restored = {}
            for pid, original in loop.original_tasks.items():
                try:
                    restored[str(pid)] = task_state(pid) == original
                except (FileNotFoundError, ProcessLookupError):
                    restored[str(pid)] = "exited"
            summary["task_restoration"] = restored
        # Rollback must precede termination so live owned task state is checked.
        workloads.close()
        summary["workload_cleanup"] = workloads.cleanup
        summary["final_scheduler_state"] = ScxController().state()
        summary["remaining_journals"] = [str(path) for path in (context.rollback_file, context.scx_rollback_file,
                                                                context.state_dir / "process_rollback.json") if path.exists()]
        summary["pinned_hooks_remaining"] = bool(probe and probe.controller.has_pinned_programs())
        if (not rollback.ok or summary["remaining_journals"] or summary["pinned_hooks_remaining"]
                or summary["final_scheduler_state"] != "disabled" or workloads.cleanup["status"] != "passed"
                or any(value is False for value in summary.get("task_restoration", {}).values())):
            summary["status"] = "failed"
        os.environ["PATH"] = old_path
        atomic_json(args.output / "summary.json", summary)
        print(json.dumps(summary, indent=2), flush=True)
    if summary["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
