from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
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
from schedx.skills.scx_skill import ScxSkill, ScxStatsSkill
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
        "ebpf_policy",
        "scx",
        "act",
        "canary_candidate",
        "verify",
    ]
    MAX_ITERATIONS = 20

    def __init__(self, context: AgentContext, max_iterations: int = MAX_ITERATIONS) -> None:
        self.context = context
        self.max_iterations = max_iterations
        self.iterations: list[LoopIteration] = []
        self.decisions: list[LoopDecision] = []
        self.engine = DecisionEngine()
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
            "canary_candidate": CanaryCandidateSkill(),
            "verify": VerifySkill(),
            "report": ReportSkill(),
            "rollback": RollbackSkill(),
            "scx_stats": ScxStatsSkill(),
            "llm_analyze": LlmAnalyzeSkill(),
            "llm_policy": LlmPolicySkill(),
        }

    def run(self, initial_phase: str = "probe") -> dict[str, Any]:
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

            result = self._execute_skill(phase, iteration)
            loop_decision = self._make_decision(phase, result, iteration)
            self.decisions.append(loop_decision)

            if not loop_decision.should_continue:
                break

            phase = loop_decision.next_phase or self._next_phase(phase)
            if phase is None:
                break

        return self._build_report()

    def run_continuous(self, interval: float = 30.0, max_rounds: int = 0) -> None:
        """Continuous agent loop: sense → decide → act → verify → repeat."""
        round_num = 0
        while True:
            round_num += 1
            if max_rounds > 0 and round_num > max_rounds:
                break

            print(f"\n{'='*50}")
            print(f"  Agent Round {round_num}")
            print(f"{'='*50}")

            self.context.data["round"] = round_num
            self.context.dry_run = False

            result = self._run_one_round(round_num)
            self.round_history.append(result)

            if self.engine.should_stop(self.round_history):
                print("Agent: optimization converged, stopping.")
                break

            print(f"Agent: sleeping {interval}s before next round...")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\nAgent: interrupted by user.")
                break

    def _run_one_round(self, round_num: int) -> dict[str, Any]:
        probe_result = self._execute_skill("probe", round_num * 100 + 1)
        if not probe_result.ok:
            return {"round": round_num, "status": "probe_failed"}

        analyze_result = self._execute_skill("analyze", round_num * 100 + 2)
        if not analyze_result.ok:
            return {"round": round_num, "status": "analyze_failed"}

        classification = self.context.data.get("classification", {})

        self._execute_skill("llm_analyze", round_num * 100 + 2)
        pressure = self.context.data.get("snapshot", {}).get("pressure", {})
        topology = self.context.data.get("topology", {})

        if self.context.data.get("llm_policy_enabled"):
            self._execute_skill("llm_policy", round_num * 100 + 3)
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

        policy_result = self._execute_skill("policy", round_num * 100 + 3)
        baseline_result = self._execute_skill("canary_baseline", round_num * 100 + 4)
        if not baseline_result.ok:
            return {"round": round_num, "status": "canary_baseline_failed"}
        scx_result = self._execute_skill("scx", round_num * 100 + 5)
        act_result = self._execute_skill("act", round_num * 100 + 6)
        candidate_result = self._execute_skill("canary_candidate", round_num * 100 + 7)
        verify_result = (
            self._execute_skill("verify", round_num * 100 + 8)
            if candidate_result.ok
            else SkillResult(False, candidate_result.message)
        )

        rollback_required = (
            not policy_result.ok
            or not act_result.ok
            or not candidate_result.ok
            or not verify_result.ok
            or bool(self.context.data.get("rollback_required"))
        )
        if rollback_required:
            rollback_result = self._execute_skill("rollback", round_num * 100 + 9)
            if rollback_result.ok:
                self.context.data.pop("rollback_required", None)

        successful = (
            policy_result.ok
            and baseline_result.ok
            and act_result.ok
            and candidate_result.ok
            and verify_result.ok
        )

        return {
            "round": round_num,
            "status": "ok" if successful else "failed_rolled_back",
            "decision": {
                "mode": agent_decision.mode,
                "target": agent_decision.target,
                "reason": agent_decision.reason,
                "confidence": agent_decision.confidence,
            },
            "act_success": act_result.ok,
            "scx_success": scx_result.ok,
            "canary_success": candidate_result.ok,
            "verify_success": verify_result.ok,
        }

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
        result = skill.run(self.context)
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
        self._record_decision_log(phase, result, iteration)
        return result

    def _make_decision(self, phase: str, result: SkillResult, iteration: int) -> LoopDecision:
        if not result.ok:
            if phase == "verify" and self.context.data.get("rollback_required"):
                return LoopDecision(True, "rollback", "canary rejected; initiating rollback")
            if phase == "act":
                return LoopDecision(True, "rollback", "execution failed; initiating rollback")
            if phase == "canary_candidate":
                return LoopDecision(True, "rollback", "candidate measurement failed; initiating rollback")
            if phase == "rollback":
                return LoopDecision(False, None, "rollback completed after failure")
            if phase == "scx":
                return LoopDecision(True, "act", "scx failed; falling back to cgroup-only")
            if phase.startswith("ebpf_"):
                return LoopDecision(True, self._next_phase(phase), f"{phase} failed; degraded mode")
            return LoopDecision(False, None, f"{phase} failed: {result.message}")

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

    def _build_report(self) -> dict[str, Any]:
        if self.iterations and self.iterations[-1].phase == "rollback":
            final_status = "rolled_back"
        elif self.iterations and self.iterations[-1].result.ok:
            final_status = "success"
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
                "rollback": self.context.data.get("rollback"),
                "decision_log": self.context.data.get("decision_log", []),
            },
        }
