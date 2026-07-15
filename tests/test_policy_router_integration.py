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


def test_explicit_mode_and_target_bypass_router(tmp_path: Path):
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
    assert "policy_route" not in context.data


def test_explicit_mode_without_target_selects_target_but_bypasses_router(tmp_path: Path):
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
    assert "policy_route" not in context.data


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


def test_agent_loop_routes_rejected_verification_to_rollback(tmp_path: Path):
    context = AgentContext(state_dir=tmp_path)
    context.data["rollback_required"] = True
    loop = AgentLoop(context)

    decision = loop._make_decision(
        "verify", SkillResult(False, "canary rejected"), iteration=5
    )

    assert decision.should_continue
    assert decision.next_phase == "rollback"
