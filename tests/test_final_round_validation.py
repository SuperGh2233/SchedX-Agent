import copy
import importlib.util
import math
import sys
from pathlib import Path

import pytest

from schedx.agent.loop import AgentLoop
from schedx.controllers.scx_controller import ScxController
from schedx.policies.verifier import CanaryVerifier


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def comparison():
    return load_script("run_final_round_comparison")


def valid_comparison_rows():
    rows = []
    for case in ("nginx", "redis", "batch"):
        for repeat in range(1, 6):
            for version in ("reference", "baseline", "candidate"):
                rows.append({"case": case, "repeat": repeat, "version": version,
                             "background_cpu_seconds_per_wall_second": 1.0, "failures": [],
                             "metrics": {"requests_per_sec": 1000, "p99_ms": 8 if version == "candidate" else 10,
                                         "events_per_second": 100}})
    return rows


def test_five_valid_pairs_support_comparison(comparison):
    result = comparison.compare(valid_comparison_rows())
    assert result["status"] == "passed"
    assert result["comparisons"]["nginx"]["p99_ms"]["valid_pair_count"] == 5
    assert result["comparisons"]["nginx"]["p99_ms"]["statistics"]["mean"] == -20.0


@pytest.mark.parametrize("fault", ["background_starvation", "client_error", "missing_pair", "nan_background", "nan_metric"])
def test_invalid_pair_cannot_count_towards_five_samples(comparison, fault):
    rows = valid_comparison_rows()
    row = next(item for item in rows if item["case"] == "nginx" and item["repeat"] == 1 and item["version"] == "candidate")
    if fault == "background_starvation":
        row["background_cpu_seconds_per_wall_second"] = 0.1
    elif fault == "client_error":
        row["failures"] = ["response_errors"]
    elif fault == "missing_pair":
        rows.remove(row)
    elif fault == "nan_background":
        row["background_cpu_seconds_per_wall_second"] = math.nan
    else:
        row["metrics"]["p99_ms"] = math.nan
    result = comparison.compare(rows)
    assert result["status"] == "not_accepted"
    assert result["comparisons"]["nginx"]["p99_ms"]["valid_pair_count"] == 4


def test_stable_throughput_regression_is_not_accepted(comparison):
    rows = valid_comparison_rows()
    for row in rows:
        if row["case"] == "batch" and row["version"] == "candidate":
            row["metrics"]["events_per_second"] = 90
    result = comparison.compare(rows)
    assert result["status"] == "not_accepted"
    assert result["comparisons"]["batch"]["events_per_second"]["stable_regression_over_5_percent"]


def test_erroring_http_output_is_not_a_valid_faster_sample(comparison):
    output = """  99% 1.00ms
1000 requests in 1.00s, 1MB read
Socket errors: connect 0, read 0, write 0, timeout 5
Non-2xx or 3xx responses: 200
Requests/sec: 1000.00
"""
    metrics, failures = comparison.client_metrics("nginx", output, 0)
    assert metrics["requests_per_sec"] == 800
    assert {"response_errors", "socket_errors"}.issubset(failures)


def test_execution_rotates_reference_and_old_new_order(comparison):
    orders = [comparison.execution_order(index) for index in range(1, 6)]
    assert all(set(order) == {"reference", "baseline", "candidate"} for order in orders)
    assert sum(order.index("baseline") < order.index("candidate") for order in orders) == 3
    assert len({order.index("reference") for order in orders}) == 3


def test_real_ipc_lifecycle_is_reaped_with_a_protocol_fixture(comparison, monkeypatch, tmp_path):
    # Exercises actual subprocess IPC and logs, not kernel attachment.
    kernel = tmp_path / "mock-kernel"
    kernel.mkdir()
    state, ops = kernel / "state", kernel / "ops"
    state.write_text("disabled")
    ops.write_text("schedx_agent")
    binary = tmp_path / "protocol-fixture"
    binary.write_text(f"""#!{sys.executable}
import sys
from pathlib import Path
state = Path({str(state)!r})
state.write_text('enabled')
print('fixture ready', flush=True)
print('schedx>', flush=True)
try:
 for line in sys.stdin:
  if line.strip() == 'quit': break
  print('policy updated', flush=True)
  print('schedx>', flush=True)
finally:
 state.write_text('disabled')
""")
    binary.chmod(0o755)
    groups = {name: tmp_path / name for name in comparison.POLICIES}
    for group in groups.values():
        group.mkdir()
    output = tmp_path / "logs"
    output.mkdir()
    monkeypatch.setattr(comparison, "ScxController", lambda **kw: ScxController(sys_root=kernel, **kw))
    session = comparison.NativeSession(binary, groups, output)
    session.__enter__()
    assert session.alive()
    assert all(row["acknowledged"] for row in session.evidence["policies"].values())
    session.close()
    assert session.evidence["output_readers_stopped"]
    assert state.read_text() == "disabled"
    assert "policy updated" in (output / "native-stdout.log").read_text()


def flow_evidence():
    measurement = {"p99_ms": 10.0, "requests_per_sec": 1000.0, "successful_requests_per_sec": 1000.0,
                   "requests_completed": 1000, "elapsed_seconds": 1.0, "non_success_responses": 0,
                   "socket_errors": {"connect": 0, "read": 0, "write": 0, "timeout": 0}}
    phases = {name: {"ok": True, "data": {"failed": 0}} for name in AgentLoop.PHASES if name != "decide"}
    evidence = {}
    for index in range(1, 11):
        stage = "stable" if index <= 3 else "interference" if index <= 6 else "recovery" if index <= 9 else "http_fault"
        evidence[index] = {"stage": stage, "scheduler_state": "enabled", "ebpf_status": "attached",
                           "phase_results": copy.deepcopy(phases), "converged": index in (3, 6, 9),
                           "snapshot": {"scope_pids": [21, 99]}, "actions": [{"target_type": "pid", "target": "21"}],
                           "execution_results": [{"status": "ok"}], "scx_results": {"successful": 1},
                           "ebpf_policy_results": [{"success": True}],
                           "classification": {"groups": {"background_noise": [{"pid": 99}] if stage == "interference" else []}},
                           "agent_decision": {"mode": "latency_first"},
                           "canary": {"baseline": copy.deepcopy(measurement), "candidate": copy.deepcopy(measurement)},
                           "canary_verdict": {"status": "accepted"},
                           "result": {"round": index, "status": "ok", "objective_status": "accepted", "improvement": 0.0,
                                      "metrics": copy.deepcopy(measurement),
                                      "decision": {"mode": "latency_first", "target": "nginx", "parameters": {}}}}
    evidence[10]["result"].update(status="failed_rolled_back", failed_phase="verify", rollback_success=True)
    evidence[10]["canary"]["candidate"]["non_success_responses"] = 1000
    evidence[10]["canary_verdict"] = {"status": "rejected", "reasons": ["response_errors"]}
    evidence[10]["phase_results"]["verify"]["ok"] = False
    evidence[10]["phase_results"]["rollback"] = {"ok": True, "data": {}}
    return evidence


def test_full_flow_assessment_requires_actual_failure_and_coverage():
    module = load_script("verify_autonomous_flow")
    evidence = flow_evidence()
    assert module.assess(evidence, 3)["status"] == "passed"
    evidence[10]["canary"]["candidate"]["non_success_responses"] = 0
    assert "http_error_not_measured_rejected_and_recovered" in module.assess(evidence, 3)["failures"]


@pytest.mark.parametrize("fault", ["missing_phase", "outside_scope", "outside_execution_scope", "partial_hooks", "missing_quality", "no_actuation", "false_convergence"])
def test_narrow_or_false_evidence_cannot_pass_full_flow_gate(fault):
    module = load_script("verify_autonomous_flow")
    evidence = flow_evidence()
    row = evidence[1]
    if fault == "missing_phase":
        row["phase_results"].pop("act")
    elif fault == "outside_scope":
        row["actions"][0]["target"] = "999"
    elif fault == "outside_execution_scope":
        row["execution_results"][0]["pid"] = 999
    elif fault == "partial_hooks":
        row["phase_results"]["ebpf_attach"]["data"]["failed"] = 1
    elif fault == "missing_quality":
        row["canary"]["candidate"].pop("socket_errors")
    elif fault == "no_actuation":
        row["execution_results"] = []
    else:
        row["converged"] = True
    assert module.assess(evidence, 3)["status"] == "failed"


def test_rejected_candidate_uses_pre_rollback_backend_evidence():
    module = load_script("verify_autonomous_flow")
    evidence = flow_evidence()
    row = evidence[1]
    row["result"].update(status="failed_rolled_back", failed_phase="verify", rollback_success=True)
    row["canary"]["candidate"]["p99_ms"] = 11
    row["canary_verdict"]["status"] = "rejected"
    row["pre_verification_state"] = {"scheduler_state": "enabled", "ebpf_status": "attached"}
    row["scheduler_state"] = "disabled"
    row["ebpf_status"] = "cleaned"
    assessment = module.assess(evidence, 3)
    assert "scheduler_inactive_round_1" not in assessment["failures"]
    assert "incomplete_ebpf_chain_round_1" not in assessment["failures"]


@pytest.mark.parametrize("fault", [None, "request_error", "missing_quality", "outside_pause", "missing_recovery", "fake_gain", "mutation"])
def test_observation_assessment_requires_health_and_prior_confirmed_recovery(fault):
    module = load_script("verify_autonomous_flow")
    evidence = flow_evidence()
    prior = evidence[1]
    prior["result"].update(status="failed_rolled_back", failed_phase="verify", rollback_success=True,
                           trial_context="context-1")
    prior["canary"]["candidate"]["p99_ms"] = 11
    prior["canary_verdict"]["status"] = "rejected"
    row = evidence[2]
    sample = row["canary"]["baseline"]
    verdict = CanaryVerifier().evaluate("latency_first", sample, sample, error_metrics_expected=True)
    row["policy_observation"] = {
        "metrics": sample, "verdict": verdict.to_dict(), "context_key": "context-1",
        "rejected_round": 1, "retry_after_round": 4, "scheduler_state": "enabled"}
    row["result"].update(objective_status="observed_healthy", observation_verified=True, noop=True,
                          improvement=None, metrics=sample)
    row["actions"] = row["execution_results"] = []
    row["phase_results"] = {phase: {"ok": True} for phase in ("probe", "analyze", "canary_baseline")}
    if fault == "request_error":
        sample["non_success_responses"] = 100
    elif fault == "missing_quality":
        sample.pop("socket_errors")
    elif fault == "outside_pause":
        row["policy_observation"]["retry_after_round"] = 1
    elif fault == "missing_recovery":
        prior["result"]["rollback_success"] = False
    elif fault == "fake_gain":
        row["result"]["improvement"] = 10
    elif fault == "mutation":
        row["actions"] = [{"target_type": "pid", "target": 21}]
    failures = module.assess(evidence, 3)["failures"]
    assert ("invalid_policy_observation_round_2" in failures) == bool(fault)
