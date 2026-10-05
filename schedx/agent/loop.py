from __future__ import annotations

import time
import uuid
import math
import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from schedx.agent.context import AgentContext
from schedx.agent.decision import DecisionEngine, Decision
from schedx.agent.skill import Skill, SkillResult
from schedx.skills.act_skill import ActSkill
from schedx.skills.analyze_skill import AnalyzeSkill
from schedx.skills.canary_skill import CanaryBaselineSkill, CanaryCandidateSkill
from schedx.skills.ebpf_skill import EbpfLoadSkill, EbpfAttachSkill, EbpfPolicySkill, EbpfStatsSkill
from schedx.skills.policy_skill import PolicySkill
from schedx.skills.probe_skill import ProbeSkill
from schedx.skills.report_skill import ReportSkill
from schedx.skills.rollback_skill import RollbackSkill
from schedx.skills.scx_skill import ScxSkill, ScxStatsSkill, ScxCgroupSkill
from schedx.skills.verify_skill import VerifySkill
from schedx.skills.llm_analyze_skill import LlmAnalyzeSkill
from schedx.skills.llm_policy_skill import LlmPolicySkill
from schedx.policies.repository import PolicyRepository
from schedx.policies.router import MODE_TO_EXPERT, SchedulerRouter, target_for_mode


@dataclass
class LoopIteration:
    iteration: int
    timestamp: str
    phase: str
    skill_name: str
    result: SkillResult
    duration_ms: float
    context_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoopDecision:
    should_continue: bool
    next_phase: str | None
    reason: str
    adjustments: dict[str, Any] = field(default_factory=dict)


class AgentLoop:
    """Autonomous agent loop with self-driving decision making.

    Flow:
      probe → analyze → [auto-decide mode+params] → policy → act → verify → [retry or done]
    """

    PHASES = [
        "probe",
        "analyze",
        "decide",
        "policy",
        "canary_baseline",
        "ebpf_load",
        "ebpf_attach",
        "scx",
        "act",
        "scx_cgroups",
        "ebpf_policy",
        "canary_candidate",
        "ebpf_stats",
        "verify",
    ]
    MAX_ITERATIONS = 20
    HISTORY_LIMIT = DecisionEngine.MAX_STABILITY_WINDOW
    LOG_LIMIT = 1000

    def __init__(self, context: AgentContext, max_iterations: int = MAX_ITERATIONS) -> None:
        self.context = context
        self.max_iterations = max_iterations
        self.iterations: list[LoopIteration] = []
        self.decisions: list[LoopDecision] = []
        self.engine = DecisionEngine(
            stability_window=context.data.get("stability_window", 3),
            stability_tolerance_percent=context.data.get("stability_tolerance_percent", 1.0),
        )
        self.round = 0
        self.round_history: list[dict] = []
        self.policy_repository = PolicyRepository(
            self.context.state_dir / "policy_repository.json"
        )
        self.policy_router = SchedulerRouter(self.policy_repository)

        self._skills: dict[str, Skill] = {
            "probe": ProbeSkill(),
            "analyze": AnalyzeSkill(),
            "policy": PolicySkill(),
            "canary_baseline": CanaryBaselineSkill(),
            "ebpf_load": EbpfLoadSkill(),
            "ebpf_attach": EbpfAttachSkill(),
            "ebpf_policy": EbpfPolicySkill(),
            "ebpf_stats": EbpfStatsSkill(),
            "scx": ScxSkill(),
            "act": ActSkill(),
            "scx_cgroups": ScxCgroupSkill(),
            "canary_candidate": CanaryCandidateSkill(),
            "verify": VerifySkill(),
            "report": ReportSkill(),
            "rollback": RollbackSkill(),
            "scx_stats": ScxStatsSkill(),
            "llm_analyze": LlmAnalyzeSkill(),
            "llm_policy": LlmPolicySkill(),
        }

    def run(self, initial_phase: str = "probe") -> dict[str, Any]:
        self.context.data["transaction_owner"] = self.context.session.session_id
        phase = initial_phase
        iteration = 0

        while iteration < self.max_iterations:
            iteration += 1

            if phase == "decide":
                decision = self._auto_decide(iteration)
                self.decisions.append(decision)
                if not decision.should_continue:
                    break
                phase = decision.next_phase or "policy"
                continue

            if phase in {"ebpf_load", "scx", "act"}:
                self.context.data["mutation_started"] = True
            result = self._execute_skill(phase, iteration)
            loop_decision = self._make_decision(phase, result, iteration)
            self.decisions.append(loop_decision)

            if not loop_decision.should_continue:
                break

            phase = loop_decision.next_phase or self._next_phase(phase)
            if phase is None:
                break

        return self._build_report()

    def run_continuous(self, interval: float = 30.0, max_rounds: int = 0) -> str:
        """Monitor indefinitely; stable objectives suppress churn, not monitoring."""
        if not math.isfinite(interval) or interval < 0 or max_rounds < 0:
            raise ValueError("interval and max_rounds must be non-negative")
        round_num, failures = 0, 0
        failure_limit = self.context.data.get("max_consecutive_failures", 3)
        if isinstance(failure_limit, bool) or not isinstance(failure_limit, int) or failure_limit < 1:
            raise ValueError("max_consecutive_failures must be a positive integer")
        self.round_history.clear()
        self._completed_rounds = 0
        self.context.data["converged"] = False
        self.context.data.pop("stop_reason", None)
        try:
            while max_rounds == 0 or round_num < max_rounds:
                round_num += 1
                self.context.data["round"] = round_num
                result = self._run_one_round(round_num)
                self.round_history.append(copy.deepcopy(result))
                self._completed_rounds += 1
                self.round_history[:] = self.round_history[-self.HISTORY_LIMIT:]
                was_stable = self.context.data["converged"]
                self.context.data["stability"] = self.engine.assess_stability(self.round_history)
                self.context.data["converged"] = self.context.data["stability"]["stable"]
                safe_rejection = (result.get("status") == "failed_rolled_back"
                                  and result.get("failed_phase") == "verify"
                                  and result.get("rollback_success") is True
                                  and result.get("canary_rejected") is True)
                failures = 0 if result["status"] == "ok" or safe_rejection else failures + 1
                if result["status"] == "rollback_failed":
                    return self._finish_continuous("rollback_failed", round_num)
                if failures >= failure_limit:
                    return self._finish_continuous("consecutive_failures", round_num)
                if self.context.data["converged"] and not was_stable:
                    print("Agent: measured objectives stable; continuing observation.")
                elif was_stable and not self.context.data["converged"]:
                    print("Agent: objective or policy changed; reassessing stability.")
                self._save_continuous_state(round_num)
                if max_rounds and round_num >= max_rounds:
                    break
                time.sleep(min(interval * (2 ** max(0, failures - 1)), max(interval, 300.0)))
        except KeyboardInterrupt:
            if self.context.data.get("mutation_started"):
                result = self._execute_skill("rollback", round_num * 100 + 99)
                if not result.ok:
                    return self._finish_continuous("rollback_failed", round_num)
            return self._finish_continuous("interrupted", round_num)
        return self._finish_continuous("max_rounds", round_num)

    def _finish_continuous(self, reason: str, rounds: int) -> str:
        self.context.data["stop_reason"] = reason
        self._save_continuous_state(rounds)
        return reason

    def _save_continuous_state(self, rounds: int) -> None:
        if self.context.session:
            self.context.session.metrics["continuous"] = {
                "round_count": rounds,
                "completed_round_count": self._completed_rounds,
                "history_limit": self.HISTORY_LIMIT,
                "log_limit": self.LOG_LIMIT,
                "round_history": self.round_history,
                "stability": self.context.data.get("stability", {}),
                "converged": self.context.data.get("converged", False),
                "stop_reason": self.context.data.get("stop_reason"),
                "phase_log": self.context.data.get("decision_log", [])[-self.LOG_LIMIT:],
            }
        self.context.save_session()

    def _run_one_round(self, round_num: int) -> dict[str, Any]:
        if self.context.data.get("rollback_required"):
            return {"round": round_num, "status": "rollback_failed", "reason": "recovery required before new actions"}
        self._reset_round()
        probe_result = self._execute_skill("probe", round_num * 100 + 1)
        if not probe_result.ok:
            return {"round": round_num, "status": "probe_failed"}

        analyze_result = self._execute_skill("analyze", round_num * 100 + 2)
        if not analyze_result.ok:
            return {"round": round_num, "status": "analyze_failed"}

        classification = self.context.data.get("classification", {})

        if self.context.data.get("llm_policy_enabled"):
            self._execute_skill("llm_analyze", round_num * 100 + 2)
        pressure = self.context.data.get("snapshot", {}).get("pressure", {})
        topology = self.context.data.get("topology", {})

        if self.context.data.get("llm_policy_enabled"):
            llm_result = self._execute_skill("llm_policy", round_num * 100 + 3)
            if not llm_result.ok:
                return {"round": round_num, "status": "llm_policy_failed"}
            raw = self.context.data["agent_decision"]
            proposal = Decision(
                raw["mode"], raw["target"], raw["parameters"], raw["reason"], raw["confidence"]
            )
            source = str(raw.get("source", "llm_policy"))
        else:
            preferred_target = self.context.data.get("target", "")
            proposal = self.engine.decide(
                classification, pressure, topology, preferred_target
            )
            source = "rule_engine"
        agent_decision = self._route_proposal(proposal, classification, source)
        print(f"Agent decision: mode={agent_decision.mode}, target={agent_decision.target}")
        print(f"  reason: {agent_decision.reason}")
        print(f"  confidence: {agent_decision.confidence:.0%}")

        results: dict[str, SkillResult] = {}
        for offset, phase in enumerate(("policy", "canary_baseline", "ebpf_load", "ebpf_attach", "scx", "act", "scx_cgroups", "ebpf_policy", "canary_candidate", "ebpf_stats", "verify"), 3):
            if phase in {"ebpf_load", "scx", "act"}:
                self.context.data["mutation_started"] = True
            result = self._execute_skill(phase, round_num * 100 + offset)
            results[phase] = result
            if phase == "policy" and result.ok and "actions" in self.context.data and not self.context.data["actions"]:
                self.context.data["execution_results"] = []
                self.context.data["execution_noop"] = True
                return {"round": round_num, "status": "ok", "noop": True, "objective_status": "unmeasured", "improvement": None}
            if not result.ok:
                if phase in {"ebpf_load", "ebpf_attach", "ebpf_stats"}:
                    self.context.data.setdefault("degraded_phases", []).append(phase)
                    continue
                if self.context.data.get("mutation_started"):
                    self.context.data["rollback_required"] = True
                    rollback = self._execute_skill("rollback", round_num * 100 + 20)
                    if rollback.ok:
                        self.context.data.pop("rollback_required", None)
                        self.context.data.pop("mutation_started", None)
                    return {"round": round_num, "status": "failed_rolled_back" if rollback.ok else "rollback_failed",
                            "failed_phase": phase, "verify_success": results.get("verify", SkillResult(False, "not run")).ok,
                            "rollback_success": rollback.ok,
                            "canary_rejected": phase == "verify"
                                and self.context.data.get("canary_verdict", {}).get("status") == "rejected"
                                and not set(self.context.data.get("canary_verdict", {}).get("reasons", [])) & {
                                    "invalid_metric_payload", "invalid_metric_value", "invalid_metric_delta", "invalid_request_quality"}}
                return {"round": round_num, "status": f"{phase}_failed", "failed_phase": phase}
        verdict = self.context.data.get("canary_verdict", {})
        self.context.data.pop("mutation_started", None)
        self.context.data["accepted_classification"] = copy.deepcopy(classification)
        self.context.data["accepted_cgroup_ids"] = [row.get("cgroup_id") for row in self.context.data.get("ebpf_policy_results", []) if row.get("cgroup_id")]
        self.context.data["accepted_ebpf_policy_results"] = copy.deepcopy(self.context.data.get("ebpf_policy_results", []))
        return {
            "round": round_num, "status": "ok", "decision": {
                "mode": agent_decision.mode, "target": agent_decision.target,
                "reason": agent_decision.reason, "confidence": agent_decision.confidence,
                "parameters": copy.deepcopy(agent_decision.parameters),
                "expert_id": self.context.data.get("policy_route", {}).get("expert_id"),
            },
            **{phase + "_success": result.ok for phase, result in results.items()},
            "canary_success": results["canary_candidate"].ok,
            "degraded_phases": self.context.data.get("degraded_phases", []),
            "objective_status": verdict.get("status", "unmeasured"),
            "metrics": self.context.data.get("canary", {}).get("candidate", {}),
            "improvement": self._objective_improvement(),
        }

    def _reset_round(self) -> None:
        self.context.data["transaction_id"] = uuid.uuid4().hex
        self.context.data["transaction_owner"] = self.context.session.session_id
        self.context.data.pop("mutation_started", None)
        for key in ("actions", "execution_results", "execution_noop", "canary", "canary_verdict", "verification", "degraded_phases"):
            self.context.data.pop(key, None)

    def _objective_improvement(self) -> float | None:
        verdict = self.context.data.get("canary_verdict", {})
        if verdict.get("status") != "accepted":
            return None
        deltas = verdict.get("deltas", {})
        objective = self.engine.objective_metric(
            str(self.context.data.get("mode")), self.context.data.get("canary", {}).get("candidate")
        )
        if objective is None:
            return None
        key = "p99_percent" if objective[0] == "p99_ms" else "requests_per_sec_percent"
        value = self.engine._finite_number(deltas.get(key))
        return -value if value is not None and key == "p99_percent" else value

    def _auto_decide(self, iteration: int) -> LoopDecision:
        classification = self.context.data.get("classification", {})
        pressure = self.context.data.get("snapshot", {}).get("pressure", {})
        topology = self.context.data.get("topology", {})

        if self.context.data.get("mode"):
            if not self.context.data.get("target"):
                self.context.data["target"] = target_for_mode(
                    str(self.context.data["mode"]), classification
                )
            mode = str(self.context.data["mode"])
            target = str(self.context.data["target"])
            expert_id = MODE_TO_EXPERT.get(mode)
            if expert_id:
                self.context.data["policy_route"] = {
                    "expert_id": expert_id,
                    "mode": mode,
                    "confidence": 1.0,
                    "scores": {expert_id: 1.0},
                    "switched": False,
                    "reason": "explicit user-selected mode; router bypassed",
                    "timestamp": time.time(),
                }
                self.context.data["agent_decision"] = {
                    "mode": mode,
                    "target": target,
                    "parameters": {},
                    "reason": "explicit user-selected mode and target",
                    "confidence": 1.0,
                    "source": "explicit_cli",
                    "expert_id": expert_id,
                }
            return LoopDecision(
                should_continue=True,
                next_phase="policy",
                reason=f"user-specified mode={mode}, target={target}",
            )

        if self.context.data.get("llm_policy_enabled"):
            result = self._execute_skill("llm_policy", iteration)
            if not result.ok:
                return LoopDecision(False, None, result.message)
            raw = self.context.data["agent_decision"]
            proposal = Decision(
                raw["mode"],
                raw["target"],
                raw["parameters"],
                raw["reason"],
                raw["confidence"],
            )
            routed = self._route_proposal(
                proposal,
                classification,
                str(raw.get("source", "llm_policy")),
            )
            return LoopDecision(
                should_continue=True,
                next_phase="policy",
                reason=f"policy selected by {raw.get('source', 'unknown')} via {self.context.data['policy_route']['expert_id']} ({routed.confidence:.0%})",
            )

        preferred_target = self.context.data.get("target", "")
        proposal = self.engine.decide(
            classification, pressure, topology, preferred_target
        )
        decision = self._route_proposal(proposal, classification, "rule_engine")

        self._record_decision_log("decide", SkillResult(True, decision.reason), iteration)

        return LoopDecision(
            should_continue=True,
            next_phase="policy",
            reason=f"auto-decided: {decision.mode} for {decision.target} (confidence={decision.confidence:.0%})",
        )

    def _route_proposal(
        self,
        proposal: Decision,
        classification: dict[str, Any],
        source: str,
    ) -> Decision:
        route = self.policy_router.route(
            classification,
            proposal.mode,
            proposal.confidence,
        )
        expert = self.policy_repository.get(route.expert_id)
        parameters = dict(expert.parameters)
        if proposal.mode == route.mode:
            parameters.update(proposal.parameters)
        target = target_for_mode(route.mode, classification, proposal.target)
        decision = Decision(
            mode=route.mode,
            target=target,
            parameters=parameters,
            reason=(
                f"{proposal.reason}; router={route.expert_id} "
                f"({route.reason})"
            ),
            confidence=route.confidence,
        )
        self.context.data["mode"] = decision.mode
        self.context.data["target"] = decision.target
        self.context.data["policy_route"] = route.to_dict()
        self.context.data["agent_decision"] = {
            "mode": decision.mode,
            "target": decision.target,
            "parameters": decision.parameters,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "source": source,
            "expert_id": route.expert_id,
        }
        return decision

    def _execute_skill(self, phase: str, iteration: int) -> SkillResult:
        skill = self._skills.get(phase)
        if skill is None:
            return SkillResult(False, f"unknown phase: {phase}")

        start_time = time.time()
        try:
            result = skill.run(self.context)
        except Exception as exc:
            result = SkillResult(False, f"{phase} failed: {exc}", {"error": str(exc)})
        duration_ms = (time.time() - start_time) * 1000

        iteration_record = LoopIteration(
            iteration=iteration,
            timestamp=datetime.now().isoformat(),
            phase=phase,
            skill_name=skill.name,
            result=result,
            duration_ms=duration_ms,
            context_snapshot=self._snapshot_context(),
        )
        self.iterations.append(iteration_record)
        self.iterations[:] = self.iterations[-self.LOG_LIMIT:]
        self._record_decision_log(phase, result, iteration)
        return result

    def _make_decision(self, phase: str, result: SkillResult, iteration: int) -> LoopDecision:
        if phase == "rollback":
            if result.ok:
                self.context.data.pop("rollback_required", None)
                self.context.data.pop("mutation_started", None)
            else:
                self.context.data["rollback_required"] = True
            return LoopDecision(False, None, "rollback completed" if result.ok else "rollback failed; recovery required")
        if not result.ok:
            if phase in {"ebpf_load", "ebpf_attach", "ebpf_stats"}:
                self.context.data.setdefault("degraded_phases", []).append(phase)
                return LoopDecision(True, self._next_phase(phase), f"{phase} failed; degraded mode")
            if self.context.data.get("mutation_started") or self.context.data.get("rollback_required"):
                self.context.data["rollback_required"] = True
                return LoopDecision(True, "rollback", f"{phase} failed; initiating rollback")
            return LoopDecision(False, None, f"{phase} failed: {result.message}")

        if phase == "policy" and "actions" in self.context.data and not self.context.data["actions"]:
            self.context.data["execution_results"] = []
            self.context.data["execution_noop"] = True
            return LoopDecision(True, "verify", "no matching actions; verifying safe no-op")
        if phase == "verify":
            return LoopDecision(False, None, "optimization round completed")

        next_phase = self._next_phase(phase)
        if next_phase is None:
            return LoopDecision(False, None, "all phases completed")
        return LoopDecision(True, next_phase, f"proceeding to {next_phase}")

    def _next_phase(self, current: str) -> str | None:
        try:
            idx = self.PHASES.index(current)
            if idx + 1 < len(self.PHASES):
                return self.PHASES[idx + 1]
        except ValueError:
            pass
        return None

    def _snapshot_context(self) -> dict[str, Any]:
        return {
            "dry_run": self.context.dry_run,
            "mode": self.context.data.get("mode"),
            "target": self.context.data.get("target"),
            "has_classification": "classification" in self.context.data,
            "has_actions": "actions" in self.context.data,
        }

    def _record_decision_log(self, phase: str, result: SkillResult, iteration: int) -> None:
        if "decision_log" not in self.context.data:
            self.context.data["decision_log"] = []
        self.context.data["decision_log"].append({
            "iteration": iteration,
            "phase": phase,
            "success": result.ok,
            "message": result.message,
            "timestamp": datetime.now().isoformat(),
        })
        self.context.data["decision_log"][:] = self.context.data["decision_log"][-self.LOG_LIMIT:]

    def _build_report(self) -> dict[str, Any]:
        if self.iterations and self.iterations[-1].phase == "rollback":
            final_status = "rolled_back" if self.iterations[-1].result.ok else "rollback_failed"
        elif self.iterations and self.iterations[-1].phase == "verify" and self.iterations[-1].result.ok:
            final_status = "degraded" if self.context.data.get("degraded_phases") else "success"
        else:
            final_status = "failed"
        return {
            "total_iterations": len(self.iterations),
            "phases_completed": [it.phase for it in self.iterations],
            "final_status": final_status,
            "agent_decisions": self.context.data.get("agent_decision"),
            "iterations": [
                {
                    "iteration": it.iteration,
                    "phase": it.phase,
                    "skill": it.skill_name,
                    "success": it.result.ok,
                    "message": it.result.message,
                    "duration_ms": it.duration_ms,
                }
                for it in self.iterations
            ],
            "decisions": [
                {
                    "should_continue": d.should_continue,
                    "next_phase": d.next_phase,
                    "reason": d.reason,
                }
                for d in self.decisions
            ],
            "context_data": {
                "mode": self.context.data.get("mode"),
                "target": self.context.data.get("target"),
                "agent_decision": self.context.data.get("agent_decision"),
                "policy_route": self.context.data.get("policy_route"),
                "canary_verdict": self.context.data.get("canary_verdict"),
                "canary": self.context.data.get("canary"),
                "execution_results": self.context.data.get("execution_results", []),
                "ebpf_load_results": self.context.data.get("ebpf_load_results", {}),
                "ebpf_attach_results": self.context.data.get("ebpf_attach_results", {}),
                "ebpf_policy_results": self.context.data.get("ebpf_policy_results", []),
                "ebpf_stats": self.context.data.get("ebpf_stats"),
                "ebpf_cleanup": self.context.data.get("ebpf_cleanup", {}),
                "rollback": self.context.data.get("rollback"),
                "decision_log": self.context.data.get("decision_log", []),
            },
        }
