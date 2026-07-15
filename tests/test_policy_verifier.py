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


def test_verifier_reports_inconclusive_when_objective_metric_is_missing():
    verdict = CanaryVerifier().evaluate(
        "throughput_first",
        baseline={},
        candidate={},
    )

    assert verdict.accepted
    assert verdict.status == "inconclusive"
    assert verdict.reasons == ["missing_objective_metrics"]

