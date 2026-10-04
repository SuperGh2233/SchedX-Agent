import json

import pytest

from schedx.agent.context import AgentContext
from schedx.agent.decision import DecisionEngine
from schedx.agent.loop import AgentLoop
from schedx.agent.skill import SkillResult
from schedx.main import build_parser, cmd_run


def measured(value=10.0, *, mode="latency_first", parameters=None):
    return {
        "status": "ok", "objective_status": "accepted", "improvement": 0.2,
        "decision": {"mode": mode, "target": "service", "parameters": parameters or {"cpu_weight": 1000}},
        "metrics": {"p99_ms": value},
    }


def test_small_gains_do_not_hide_large_changes_in_absolute_latency():
    assessment = DecisionEngine().assess_stability([measured(v) for v in (10, 50, 2)])
    assert not assessment["stable"]
    assert assessment["reason"] == "objective_variation"
    assert assessment["values"] == [10, 50, 2]


def test_stability_uses_configured_window_and_tolerance():
    engine = DecisionEngine(stability_window=4, stability_tolerance_percent=2)
    rows = [measured(v) for v in (100, 100.5, 101, 100.2)]
    assert not engine.should_stop(rows[:3])
    assert engine.should_stop(rows)
    assert not DecisionEngine(stability_window=4, stability_tolerance_percent=0.5).should_stop(rows)


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), -1])
def test_invalid_objective_samples_never_establish_stability(value):
    rows = [measured(), measured(), measured(value)]
    assert not DecisionEngine().should_stop(rows)


def test_policy_change_and_missing_objective_identity_reset_stability():
    engine = DecisionEngine()
    rows = [measured(), measured(), measured(parameters={"cpu_weight": 2000})]
    assert engine.assess_stability(rows)["reason"] == "policy_changed"
    rows[-1]["decision"] = {}
    assert engine.assess_stability(rows)["reason"] == "missing_objective_identity"


@pytest.mark.parametrize("value", [True, None, float("nan")])
def test_invalid_improvement_does_not_establish_stability(value):
    rows = [measured() for _ in range(3)]
    rows[-1]["improvement"] = value
    assert not DecisionEngine().should_stop(rows)


def test_balanced_mode_can_observe_latency_when_throughput_is_absent():
    rows = [measured(mode="balanced") for _ in range(3)]
    assert DecisionEngine().assess_stability(rows)["metric"] == "p99_ms"
    assert DecisionEngine().should_stop(rows)


def test_throughput_stability_and_metric_changes_are_checked():
    rows = [measured(mode="throughput_first") for _ in range(3)]
    for row, value in zip(rows, (1000, 1002, 998)):
        row["metrics"] = {"requests_per_sec": value}
    assert DecisionEngine().should_stop(rows)
    rows[-1]["metrics"]["requests_per_sec"] = 2000
    assert not DecisionEngine().should_stop(rows)
    rows = [measured(mode="balanced") for _ in range(3)]
    rows[-1]["metrics"]["requests_per_sec"] = 1000
    assert DecisionEngine().assess_stability(rows)["reason"] == "objective_metric_changed"


@pytest.mark.parametrize("kwargs", [{"stability_window": 1}, {"stability_window": 129}, {"stability_window": True}, {"stability_tolerance_percent": float("nan")}, {"stability_tolerance_percent": float("inf")}])
def test_invalid_stability_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        DecisionEngine(**kwargs)


@pytest.mark.parametrize("args", [["--stable-window", "1"], ["--stable-window", "129"], ["--stable-tolerance", "nan"], ["--stable-tolerance", "inf"], ["--interval", "nan"]])
def test_cli_rejects_invalid_monitor_configuration_before_execution(args):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", *args])


def test_cli_passes_stability_settings_to_the_loop(tmp_path, monkeypatch):
    args = build_parser().parse_args(["run", "--stable-window", "5", "--stable-tolerance", "2", "--state-dir", str(tmp_path)])
    seen = {}

    def run(loop, **kwargs):
        seen.update(window=loop.engine.stability_window, tolerance=loop.engine.stability_tolerance_percent)
        return "max_rounds"

    monkeypatch.setattr(AgentLoop, "run_continuous", run)
    assert cmd_run(args) == 0
    assert seen == {"window": 5, "tolerance": 2}


def test_stability_is_cleared_on_load_change_and_recorded_at_exit(tmp_path, monkeypatch):
    loop = AgentLoop(AgentContext(state_dir=tmp_path))
    values = (10, 10, 10, 50, 50, 50)
    states = []

    def sample(index):
        return {"round": index, **measured(values[index - 1])}

    def after_round(_):
        states.append(loop.context.data["converged"])

    monkeypatch.setattr(loop, "_run_one_round", sample)
    monkeypatch.setattr("schedx.agent.loop.time.sleep", after_round)
    assert loop.run_continuous(interval=0, max_rounds=6) == "max_rounds"
    assert states == [False, False, True, False, False]
    saved = json.loads(loop.context.session_file.read_text())["metrics"]["continuous"]
    assert saved["converged"] and saved["stop_reason"] == "max_rounds"
    assert saved["stability"]["values"] == [50, 50, 50]
    assert saved["completed_round_count"] == 6


def test_failed_round_and_final_stop_reason_are_persisted(tmp_path, monkeypatch):
    loop = AgentLoop(AgentContext(state_dir=tmp_path))
    monkeypatch.setattr(loop, "_run_one_round", lambda i: {"round": i, "status": "probe_failed"})
    monkeypatch.setattr("schedx.agent.loop.time.sleep", lambda _: None)
    assert loop.run_continuous(interval=0) == "consecutive_failures"
    saved = json.loads(loop.context.session_file.read_text())["metrics"]["continuous"]
    assert saved["stop_reason"] == "consecutive_failures" and not saved["converged"]
    assert len(saved["round_history"]) == 3


def test_long_monitor_history_and_phase_logs_are_bounded(tmp_path, monkeypatch):
    loop = AgentLoop(AgentContext(state_dir=tmp_path))
    shared = measured()

    def sample(index):
        shared["round"] = index
        for step in range(10):
            loop._record_decision_log("probe", SkillResult(True, str(index)), index * 100 + step)
        return shared

    monkeypatch.setattr(loop, "_run_one_round", sample)
    monkeypatch.setattr("schedx.agent.loop.time.sleep", lambda _: None)
    loop.run_continuous(interval=0, max_rounds=150)
    saved = json.loads(loop.context.session_file.read_text())["metrics"]["continuous"]
    assert saved["completed_round_count"] == 150
    assert len(saved["round_history"]) == 128 and saved["round_history"][0]["round"] == 23
    assert len(saved["phase_log"]) == len(loop.context.data["decision_log"]) == 1000
    shared["metrics"]["p99_ms"] = 10000
    assert saved["round_history"][-1]["metrics"]["p99_ms"] == 10
    assert loop.round_history[-1]["metrics"]["p99_ms"] == 10


def test_interrupt_records_completed_and_attempted_round_counts(tmp_path, monkeypatch):
    loop = AgentLoop(AgentContext(state_dir=tmp_path))

    def sample(index):
        if index == 3:
            raise KeyboardInterrupt()
        return measured()

    monkeypatch.setattr(loop, "_run_one_round", sample)
    monkeypatch.setattr("schedx.agent.loop.time.sleep", lambda _: None)
    assert loop.run_continuous(interval=0) == "interrupted"
    saved = json.loads(loop.context.session_file.read_text())["metrics"]["continuous"]
    assert saved["round_count"] == 3 and saved["completed_round_count"] == 2


def test_balanced_round_uses_its_measured_latency_delta(tmp_path):
    context = AgentContext(state_dir=tmp_path, data={
        "mode": "balanced", "canary": {"candidate": {"p99_ms": 10}},
        "canary_verdict": {"status": "accepted", "deltas": {"p99_percent": -0.5}},
    })
    assert AgentLoop(context)._objective_improvement() == 0.5
