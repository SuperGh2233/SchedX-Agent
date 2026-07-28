from pathlib import Path

from schedx.agent.context import AgentContext
from schedx.agent.decision import Decision
from schedx.agent.loop import AgentLoop
from schedx.agent.skill import SkillResult
from schedx.policies.repository import PolicyRepository
from schedx.skills.verify_skill import VerifySkill


def mixed_classification() -> dict:
    return {
        "overall": "mixed",
        "groups": {
            "latency_sensitive": [
                {"pid": 10, "comm": "nginx", "cpu_percent": 20.0}
            ],
            "batch_compute": [],
            "background_noise": [
                {"pid": 20, "comm": "stress-ng", "cpu_percent": 99.0}
            ],
            "unknown": [],
        },
    }


def test_agent_loop_routes_automatic_proposal_and_records_metadata(tmp_path: Path):
    context = AgentContext(state_dir=tmp_path)
    loop = AgentLoop(context)
    proposal = Decision(
        mode="latency_first",
        target="nginx",
        parameters={"cpu_weight": 8000},
        reason="mixed workload",
        confidence=0.85,
    )

    routed = loop._route_proposal(proposal, mixed_classification(), "rule_engine")

    assert routed.mode == "latency_first"
    assert routed.target == "nginx"
    assert context.data["agent_decision"]["expert_id"] == "latency_guard"
    assert context.data["agent_decision"]["source"] == "rule_engine"
    assert context.data["policy_route"]["expert_id"] == "latency_guard"
    assert context.data["mode"] == "latency_first"
    report = loop._build_report()
    assert report["context_data"]["policy_route"]["scores"]["latency_guard"] > 0.5


def test_explicit_mode_and_target_records_route_without_changing_parameters(tmp_path: Path):
    context = AgentContext(state_dir=tmp_path)
    context.data.update(
        {
            "classification": mixed_classification(),
            "mode": "latency_first",
            "target": "nginx",
        }
    )
    loop = AgentLoop(context)

    result = loop._auto_decide(1)

    assert result.should_continue
    assert result.next_phase == "policy"
    assert context.data["policy_route"]["expert_id"] == "latency_guard"
    assert context.data["policy_route"]["reason"].startswith("explicit user-selected")
    assert context.data["agent_decision"] == {
        "mode": "latency_first",
        "target": "nginx",
        "parameters": {},
        "reason": "explicit user-selected mode and target",
        "confidence": 1.0,
        "source": "explicit_cli",
        "expert_id": "latency_guard",
    }


def test_explicit_mode_without_target_selects_target_and_records_route(tmp_path: Path):
    observed = mixed_classification()
    observed["groups"]["batch_compute"] = [
        {"pid": 30, "comm": "make", "cpu_percent": 90.0}
    ]
    context = AgentContext(state_dir=tmp_path)
    context.data.update(
        {
            "classification": observed,
            "mode": "throughput_first",
        }
    )
    loop = AgentLoop(context)

    result = loop._auto_decide(1)

    assert result.should_continue
    assert context.data["mode"] == "throughput_first"
    assert context.data["target"] == "make"
    assert context.data["policy_route"]["expert_id"] == "throughput_boost"
    assert context.data["agent_decision"]["source"] == "explicit_cli"


def test_rejected_canary_sets_rollback_and_records_outcome(tmp_path: Path):
    context = AgentContext(state_dir=tmp_path)
    context.data.update(
        {
            "mode": "latency_first",
            "execution_results": [{"status": "ok"}],
            "policy_route": {"expert_id": "latency_guard"},
            "canary": {
                "baseline": {"p99_ms": 10.0},
                "candidate": {"p99_ms": 12.0},
                "nr_rejected": 0,
                "background_share": 0.2,
            },
        }
    )

    result = VerifySkill().run(context)

    assert not result.ok
    assert context.data["rollback_required"]
    assert context.data["canary_verdict"]["status"] == "rejected"
    outcome = PolicyRepository(tmp_path / "policy_repository.json").outcome_for(
        "latency_guard"
    )
    assert outcome.rejects == 1


def test_inconclusive_canary_is_not_recorded_as_accept(tmp_path: Path):
    context = AgentContext(state_dir=tmp_path)
    context.data.update(
        {
            "mode": "latency_first",
            "execution_results": [{"status": "ok"}],
            "policy_route": {"expert_id": "latency_guard"},
            "canary": {"baseline": {}, "candidate": {}},
        }
    )

    result = VerifySkill().run(context)

    assert result.ok
    assert context.data["canary_verdict"]["status"] == "inconclusive"
    assert "rollback_required" not in context.data
    outcome = PolicyRepository(tmp_path / "policy_repository.json").outcome_for(
        "latency_guard"
    )
    assert outcome.accepts == 0
    assert outcome.rejects == 0
    assert outcome.inconclusive == 1


def test_invalid_programmatic_canary_config_requests_rollback(tmp_path: Path):
    context = AgentContext(state_dir=tmp_path)
    context.data.update(
        {
            "mode": "isolate_background",
            "execution_results": [{"status": "ok"}],
            "canary_min_background_retention": 2.0,
            "canary": {"baseline": {}, "candidate": {}},
        }
    )

    result = VerifySkill().run(context)

    assert not result.ok
    assert context.data["rollback_required"] is True
    assert context.data["canary_verdict"]["status"] == "invalid_config"


def test_agent_loop_routes_rejected_verification_to_rollback(tmp_path: Path):
    context = AgentContext(state_dir=tmp_path)
    context.data["rollback_required"] = True
    loop = AgentLoop(context)

    decision = loop._make_decision(
        "verify", SkillResult(False, "canary rejected"), iteration=5
    )

    assert decision.should_continue
    assert decision.next_phase == "rollback"


def test_agent_report_exposes_rollback_evidence(tmp_path: Path):
    context = AgentContext(state_dir=tmp_path)
    context.data["rollback"] = {
        "restored": 1,
        "groups_removed": 1,
        "scx_entries": [{"pid": 20, "status": "removed"}],
    }

    report = AgentLoop(context)._build_report()

    assert report["context_data"]["rollback"] == context.data["rollback"]


def test_continuous_round_keeps_accepted_policy_active(tmp_path: Path):
    context = AgentContext(state_dir=tmp_path)
    loop = AgentLoop(context)
    phases: list[str] = []

    def execute(phase: str, iteration: int) -> SkillResult:
        phases.append(phase)
        if phase == "analyze":
            context.data["classification"] = mixed_classification()
        return SkillResult(True, f"{phase} ok")

    loop._execute_skill = execute

    result = loop._run_one_round(1)

    assert result["status"] == "ok"
    assert result["verify_success"] is True
    assert "rollback" not in phases


def test_continuous_round_rolls_back_rejected_canary(tmp_path: Path):
    context = AgentContext(state_dir=tmp_path)
    loop = AgentLoop(context)
    phases: list[str] = []

    def execute(phase: str, iteration: int) -> SkillResult:
        phases.append(phase)
        if phase == "analyze":
            context.data["classification"] = mixed_classification()
        if phase == "verify":
            context.data["rollback_required"] = True
            return SkillResult(False, "canary rejected")
        return SkillResult(True, f"{phase} ok")

    loop._execute_skill = execute

    result = loop._run_one_round(1)

    assert result["status"] == "failed_rolled_back"
    assert result["verify_success"] is False
    assert phases[-1] == "rollback"


def test_continuous_round_clears_rollback_request_after_rollback(tmp_path: Path):
    context = AgentContext(state_dir=tmp_path)
    loop = AgentLoop(context)
    phases: list[str] = []
    active_round = 1

    def execute(phase: str, iteration: int) -> SkillResult:
        phases.append(phase)
        if phase == "analyze":
            context.data["classification"] = mixed_classification()
        if phase == "verify" and active_round == 1:
            context.data["rollback_required"] = True
            return SkillResult(False, "canary rejected")
        return SkillResult(True, f"{phase} ok")

    loop._execute_skill = execute

    first = loop._run_one_round(1)
    active_round = 2
    second = loop._run_one_round(2)

    assert first["status"] == "failed_rolled_back"
    assert second["status"] == "ok"
    assert phases.count("rollback") == 1
    assert "rollback_required" not in context.data
