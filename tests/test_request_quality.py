import math

import pytest

from schedx.agent.context import AgentContext
from schedx.agent.decision import DecisionEngine
from schedx.benchmark.wrk_parser import parse_wrk_output
from schedx.main import build_parser, configure_canary
from schedx.policies.verifier import CanaryVerifier
from schedx.probes.slo_probe import WrkCanaryConfig
from schedx.skills.verify_skill import VerifySkill


def sample(*, requests=1000, seconds=1.0, response_errors=0, socket_errors=0):
    return {
        "p99_ms": 9.0,
        "requests_per_sec": requests / seconds,
        "requests_completed": requests,
        "elapsed_seconds": seconds,
        "non_success_responses": response_errors,
        "socket_errors": {
            "connect": 0,
            "read": 0,
            "write": 0,
            "timeout": socket_errors,
        },
    }


@pytest.mark.parametrize(
    "name",
    [
        "max_regression_percent",
        "min_background_share",
        "min_background_retention",
        "min_p99_improvement_percent",
        "max_response_error_rate",
        "max_response_error_increase",
        "max_socket_errors_per_second",
        "max_socket_error_increase",
    ],
)
@pytest.mark.parametrize("value", [math.nan, math.inf, True, -1.0])
def test_verifier_rejects_invalid_configuration(name, value):
    with pytest.raises(ValueError):
        CanaryVerifier(**{name: value})


@pytest.mark.parametrize(
    "name",
    [
        "min_background_share",
        "min_background_retention",
        "max_response_error_rate",
        "max_response_error_increase",
    ],
)
def test_verifier_rejects_ratios_above_one(name):
    with pytest.raises(ValueError):
        CanaryVerifier(**{name: 1.01})


def test_wrk_parser_keeps_response_and_socket_counts_separate():
    output = """  99% 9.00ms
1000 requests in 2.00s, 1MB read
Socket errors: connect 1, read 2, write 3, timeout 4
Non-2xx or 3xx responses: 20
Requests/sec: 500.00
"""
    parsed = parse_wrk_output(output)
    assert parsed["requests_completed"] == 1000
    assert parsed["elapsed_seconds"] == 2.0
    assert parsed["socket_errors"] == {
        "connect": 1,
        "read": 2,
        "write": 3,
        "timeout": 4,
    }
    assert parsed["non_success_responses"] == 20
    assert parsed["successful_requests_per_sec"] == 490.0
    assert parsed["response_error_rate"] == 0.02
    assert parsed["socket_errors_per_second"] == 5.0
    assert DecisionEngine.objective_metric("throughput_first", parsed) == ("requests_per_sec", 490.0)
    verdict = CanaryVerifier().evaluate("latency_first", sample(), parsed)
    assert not verdict.accepted
    assert {"response_errors", "socket_errors"}.issubset(verdict.reasons)


def test_wrk_parser_only_infers_zero_errors_from_completed_summary():
    parsed = parse_wrk_output("1000 requests in 1.50m, 1MB read\nRequests/sec: 11.11\n")
    assert parsed["elapsed_seconds"] == 90.0
    assert parsed["socket_errors"]["timeout"] == 0
    assert parsed["non_success_responses"] == 0
    assert "socket_errors" not in parse_wrk_output("unrelated text")


@pytest.mark.parametrize(
    "line",
    [
        "Socket errors: connect 0, read 0, write 0, timeout nan",
        "Non-2xx or 3xx responses: -2",
        "Non-2xx or 3xx responses: 2.5",
    ],
)
def test_malformed_wrk_errors_cannot_be_inferred_as_zero(line):
    parsed = parse_wrk_output(f"1000 requests in 1.00s, 1MB read\n{line}\n")
    assert parsed["request_quality_invalid"] is True
    assert not CanaryVerifier().evaluate("balanced", sample(), parsed).accepted


@pytest.mark.parametrize(
    "candidate",
    [
        {"p99_ms": 8, "socket_errors": {"timeout": 100}},
        sample(response_errors=1),
        sample(socket_errors=1),
    ],
)
def test_improved_latency_does_not_excuse_request_errors(candidate):
    verdict = CanaryVerifier().evaluate("latency_first", sample(), candidate)
    assert not verdict.accepted
    assert verdict.status == "rejected"


@pytest.mark.parametrize(
    "field,value",
    [
        ("requests_completed", True),
        ("requests_completed", 1.5),
        ("elapsed_seconds", 0),
        ("elapsed_seconds", math.nan),
        ("non_success_responses", 1001),
        ("non_success_responses", -1),
        ("socket_errors", {"connect": 0, "read": 0, "write": 0, "timeout": math.inf}),
    ],
)
def test_invalid_quality_is_rejected(field, value):
    candidate = sample()
    candidate[field] = value
    verdict = CanaryVerifier().evaluate("balanced", sample(), candidate)
    assert verdict.reasons == ["invalid_request_quality"]


def test_missing_required_quality_is_inconclusive_and_not_accepted():
    verdict = CanaryVerifier().evaluate(
        "latency_first",
        {"p99_ms": 10},
        {"p99_ms": 8},
        error_metrics_expected=True,
    )
    assert not verdict.accepted
    assert verdict.status == "inconclusive"
    assert verdict.reasons == ["missing_request_quality"]


def test_candidate_without_completed_responses_cannot_pass():
    verdict = CanaryVerifier().evaluate("balanced", sample(), sample(requests=0))
    assert not verdict.accepted
    assert "no_completed_requests" in verdict.reasons


def test_empty_baseline_cannot_establish_improvement():
    verdict = CanaryVerifier().evaluate("latency_first", sample(requests=0), sample())
    assert not verdict.accepted
    assert verdict.status == "inconclusive"
    assert "no_baseline_requests" in verdict.reasons


def test_successful_throughput_drives_gate_when_response_errors_are_allowed():
    verdict = CanaryVerifier(
        max_response_error_rate=0.3,
        max_response_error_increase=0.3,
    ).evaluate("latency_first", sample(), sample(response_errors=200))
    assert not verdict.accepted
    assert "throughput_regression" in verdict.reasons
    assert verdict.deltas["raw_requests_per_sec_percent"] == 0.0
    assert verdict.deltas["successful_requests_per_sec_percent"] == -20.0


def test_socket_events_use_elapsed_time_and_not_completed_response_denominator():
    verifier = CanaryVerifier(
        max_socket_errors_per_second=2.0,
        max_socket_error_increase=0.0,
    )
    verdict = verifier.evaluate(
        "balanced",
        sample(requests=1000, seconds=1, socket_errors=2),
        sample(requests=10000, seconds=10, socket_errors=20),
    )
    assert verdict.accepted
    assert verdict.deltas["socket_errors_per_second_increase"] == 0.0
    assert verdict.deltas["response_error_rate_increase"] == 0.0


def test_allowed_absolute_error_rate_still_rejects_increase():
    verdict = CanaryVerifier(max_response_error_rate=0.1).evaluate(
        "balanced",
        sample(),
        sample(response_errors=1),
    )
    assert not verdict.accepted
    assert "response_errors_increased" in verdict.reasons


def test_verify_skill_marks_real_canary_missing_quality_for_rollback(
    monkeypatch, tmp_path
):
    context = AgentContext(state_dir=tmp_path)
    context.data.update(
        canary_config={"url": "http://127.0.0.1/"},
        mode="latency_first",
        execution_results=[{"status": "ok"}],
        canary={"baseline": {"p99_ms": 10}, "candidate": {"p99_ms": 8}},
    )
    monkeypatch.setattr(VerifySkill, "_check_system_state", lambda self: {})
    assert not VerifySkill().run(context).ok
    assert context.data["rollback_required"] is True
    assert context.data["canary_verdict"]["status"] == "inconclusive"


def test_cli_error_limits_are_validated_and_propagated():
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--canary-url",
            "http://127.0.0.1/",
            "--canary-max-response-error-rate",
            "0.01",
            "--canary-max-socket-errors-per-second",
            "2",
        ]
    )
    context = AgentContext()
    configure_canary(context, args)
    assert context.data["canary_error_limits"]["max_response_error_rate"] == 0.01
    assert context.data["canary_error_limits"]["max_socket_errors_per_second"] == 2.0
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--canary-max-socket-errors-per-second", "nan"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration", True),
        ("duration", math.inf),
        ("connections", 1.5),
        ("threads", 0),
        ("url", None),
    ],
)
def test_programmatic_wrk_config_rejects_invalid_values(field, value):
    config = {"url": "http://127.0.0.1/", field: value}
    with pytest.raises(ValueError):
        WrkCanaryConfig(**config)


def test_finite_metrics_cannot_overflow_reported_deltas():
    verdict = CanaryVerifier().evaluate(
        "latency_first", {"p99_ms": 1e-308}, {"p99_ms": 1e308}
    )
    assert not verdict.accepted
    assert verdict.reasons == ["invalid_metric_delta"]
    assert verdict.deltas == {}


def test_raw_delta_overflow_cannot_contaminate_valid_successful_rates():
    baseline, candidate = sample(), sample()
    baseline["requests_per_sec"] = 1e-308
    candidate["requests_per_sec"] = 1e308
    verdict = CanaryVerifier().evaluate("balanced", baseline, candidate)
    assert verdict.reasons == ["invalid_metric_delta"]
    assert verdict.deltas == {}
