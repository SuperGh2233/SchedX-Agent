import math

from schedx.policies.verifier import CanaryVerifier


def test_verifier_rejects_latency_p99_regression():
    verdict = CanaryVerifier(max_regression_percent=5.0).evaluate(
        "latency_first",
        baseline={"p99_ms": 10.0, "requests_per_sec": 1000.0},
        candidate={"p99_ms": 11.0, "requests_per_sec": 1000.0},
    )

    assert not verdict.accepted
    assert verdict.status == "rejected"
    assert "p99_regression" in verdict.reasons
    assert verdict.deltas["p99_percent"] == 10.0


def test_verifier_rejects_throughput_regression():
    verdict = CanaryVerifier(max_regression_percent=5.0).evaluate(
        "throughput_first",
        baseline={"requests_per_sec": 1000.0},
        candidate={"requests_per_sec": 900.0},
    )

    assert not verdict.accepted
    assert "throughput_regression" in verdict.reasons
    assert verdict.deltas["requests_per_sec_percent"] == -10.0


def test_verifier_rejects_sched_ext_rejections():
    verdict = CanaryVerifier().evaluate(
        "balanced",
        baseline={"p99_ms": 10.0},
        candidate={"p99_ms": 9.0},
        nr_rejected=1,
    )

    assert not verdict.accepted
    assert "sched_ext_rejected_tasks" in verdict.reasons


def test_verifier_rejects_background_starvation():
    verdict = CanaryVerifier(min_background_share=0.1).evaluate(
        "latency_first",
        baseline={"p99_ms": 10.0},
        candidate={"p99_ms": 8.0},
        background_share=0.05,
    )

    assert not verdict.accepted
    assert "background_starvation" in verdict.reasons


def test_verifier_accepts_latency_improvement():
    verdict = CanaryVerifier().evaluate(
        "latency_first",
        baseline={"p99_ms": 10.0, "requests_per_sec": 1000.0},
        candidate={"p99_ms": 8.0, "requests_per_sec": 1050.0},
        background_share=0.2,
    )

    assert verdict.accepted
    assert verdict.status == "accepted"
    assert verdict.reasons == []
    assert verdict.deltas["p99_percent"] == -20.0


def test_verifier_rejects_background_progress_retention_regression():
    verdict = CanaryVerifier(min_background_retention=0.25).evaluate(
        "latency_first",
        baseline={"p99_ms": 10.0},
        candidate={"p99_ms": 8.0},
        background_retention=0.10,
        background_expected=True,
    )

    assert not verdict.accepted
    assert "background_progress_regression" in verdict.reasons
    assert verdict.deltas["background_retention_percent"] == 10.0


def test_latency_verifier_rejects_lower_latency_caused_by_rps_regression():
    verdict = CanaryVerifier(max_regression_percent=5.0).evaluate(
        "latency_first",
        baseline={"p99_ms": 10.0, "requests_per_sec": 1000.0},
        candidate={"p99_ms": 8.0, "requests_per_sec": 800.0},
    )

    assert not verdict.accepted
    assert "throughput_regression" in verdict.reasons


def test_verifier_reports_inconclusive_when_objective_metric_is_missing():
    verdict = CanaryVerifier().evaluate(
        "throughput_first",
        baseline={},
        candidate={},
    )

    assert verdict.accepted
    assert verdict.status == "inconclusive"
    assert verdict.reasons == ["missing_objective_metrics"]


def test_verifier_rejects_non_mapping_metric_payload():
    verdict = CanaryVerifier().evaluate(
        "latency_first",
        baseline=None,
        candidate=None,
    )

    assert not verdict.accepted
    assert verdict.status == "rejected"
    assert verdict.reasons == ["invalid_metric_payload"]


def test_verifier_rejects_non_finite_metric():
    verdict = CanaryVerifier().evaluate(
        "latency_first",
        baseline={"p99_ms": 10.0},
        candidate={"p99_ms": math.nan},
    )

    assert not verdict.accepted
    assert verdict.status == "rejected"
    assert verdict.reasons == ["invalid_metric_value"]


def test_verifier_accepts_background_retention_above_one():
    verdict = CanaryVerifier(min_background_retention=1.0).evaluate(
        "latency_first",
        baseline={"p99_ms": 10.0, "requests_per_sec": 100.0},
        candidate={"p99_ms": 5.0, "requests_per_sec": 101.0},
        background_share=0.2,
        background_retention=1.25,
        background_expected=True,
    )

    assert verdict.accepted
    assert verdict.status == "accepted"
    assert verdict.deltas["background_retention_percent"] == 125.0


def test_verifier_rejects_when_required_p99_improvement_is_not_met():
    verdict = CanaryVerifier(min_p99_improvement_percent=90.0).evaluate(
        "latency_first",
        baseline={"p99_ms": 10.0, "requests_per_sec": 100.0},
        candidate={"p99_ms": 5.0, "requests_per_sec": 100.0},
    )

    assert not verdict.accepted
    assert verdict.status == "rejected"
    assert "insufficient_p99_improvement" in verdict.reasons
    assert verdict.deltas["p99_percent"] == -50.0
