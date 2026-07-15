from pathlib import Path

from schedx.policies.repository import PolicyRepository
from schedx.policies.router import SchedulerRouter, target_for_mode


def classification(*types: str) -> dict:
    groups = {
        "latency_sensitive": [],
        "batch_compute": [],
        "background_noise": [],
        "unknown": [],
    }
    samples = {
        "latency_sensitive": {"pid": 10, "comm": "nginx", "cpu_percent": 20.0},
        "batch_compute": {"pid": 20, "comm": "make", "cpu_percent": 90.0},
        "background_noise": {"pid": 30, "comm": "stress-ng", "cpu_percent": 99.0},
        "unknown": {"pid": 40, "comm": "worker", "cpu_percent": 2.0},
    }
    for workload_type in types:
        groups[workload_type].append(samples[workload_type])
    active = [key for key in types if key != "unknown"]
    overall = "mixed" if len(set(active)) > 1 else (active[0] if active else "unknown")
    return {"overall": overall, "groups": groups}


def test_router_selects_latency_guard_for_mixed_service_and_noise(tmp_path: Path):
    router = SchedulerRouter(PolicyRepository(tmp_path / "policies.json"))

    decision = router.route(
        classification("latency_sensitive", "background_noise"),
        proposed_mode="latency_first",
        proposed_confidence=0.85,
        now=100.0,
    )

    assert decision.expert_id == "latency_guard"
    assert decision.mode == "latency_first"
    assert decision.confidence >= 0.5


def test_router_selects_throughput_expert_for_batch(tmp_path: Path):
    router = SchedulerRouter(PolicyRepository(tmp_path / "policies.json"))

    decision = router.route(
        classification("batch_compute"),
        proposed_mode="throughput_first",
        proposed_confidence=0.85,
        now=100.0,
    )

    assert decision.expert_id == "throughput_boost"
    assert decision.mode == "throughput_first"


def test_router_uses_balanced_when_learned_scores_are_low_confidence(tmp_path: Path):
    router = SchedulerRouter(
        PolicyRepository(tmp_path / "policies.json"),
        confidence_threshold=0.40,
    )

    decision = router.route(
        classification("unknown"),
        proposed_mode="balanced",
        proposed_confidence=0.1,
        expert_scores={
            "latency_guard": 0.26,
            "throughput_boost": 0.25,
            "background_isolation": 0.25,
            "balanced": 0.24,
        },
        now=100.0,
    )

    assert decision.expert_id == "balanced"
    assert decision.reason == "low_confidence_fallback"
    assert not decision.switched


def test_router_holds_current_expert_during_cooldown(tmp_path: Path):
    router = SchedulerRouter(
        PolicyRepository(tmp_path / "policies.json"),
        window_size=1,
        cooldown_seconds=10.0,
    )
    first = router.route(
        classification("latency_sensitive"),
        "latency_first",
        0.9,
        now=100.0,
    )

    second = router.route(
        classification("batch_compute"),
        "throughput_first",
        0.9,
        now=101.0,
    )

    assert first.expert_id == "latency_guard"
    assert second.expert_id == "latency_guard"
    assert second.reason == "cooldown_hold"
    assert not second.switched


def test_router_switches_after_cooldown_with_sustained_batch_samples(tmp_path: Path):
    router = SchedulerRouter(
        PolicyRepository(tmp_path / "policies.json"),
        window_size=2,
        decay=0.5,
        cooldown_seconds=5.0,
    )
    router.route(classification("latency_sensitive"), "latency_first", 0.9, now=100.0)
    router.route(classification("batch_compute"), "throughput_first", 0.9, now=102.0)

    decision = router.route(
        classification("batch_compute"),
        "throughput_first",
        0.9,
        now=106.0,
    )

    assert decision.expert_id == "throughput_boost"
    assert decision.switched
    assert decision.reason == "higher_weighted_score"


def test_target_selection_matches_routed_mode():
    observed = classification("latency_sensitive", "batch_compute", "background_noise")

    assert target_for_mode("latency_first", observed) == "nginx"
    assert target_for_mode("throughput_first", observed) == "make"
    assert target_for_mode("isolate_background", observed) == "stress-ng"

