import copy

import pytest

from schedx.agent.context import AgentContext
from schedx.agent.decision import Decision, DecisionEngine
from schedx.agent.loop import AgentLoop
from schedx.agent.skill import SkillResult
from schedx.policies.trials import trial_context_key
from schedx.tool_runner import ToolCallRunner, recommend_next_hint
from schedx.controllers.scx_controller import ScxController


def metrics(p99=1.0, errors=0):
    return {"p99_ms": p99, "requests_per_sec": 1000.0, "requests_completed": 1000,
            "elapsed_seconds": 1.0, "non_success_responses": errors,
            "socket_errors": dict.fromkeys(("connect", "read", "write", "timeout"), 0),
            "background_cpu_ticks": 0, "background_cpu_share": 0.0}


def context_data():
    return {"classification": {"groups": {"latency_sensitive": [{"pid": 42, "comm": "nginx", "start_time": 100}]}},
            "snapshot": {"pressure": {}}, "scope_pids": [42],
            "canary_config": {"url": "http://127.0.0.1/", "duration": 1}}


@pytest.mark.parametrize("change", ["pid", "start_time", "pressure", "parameters", "scope", "measurement", "generation"])
def test_rejection_feedback_is_confined_to_its_exact_workload_context(change):
    data = context_data()
    decision = Decision("latency_first", "nginx", {"cpu_weight": 1000})
    before = trial_context_key(decision, data, 0)
    generation = 0
    if change in {"pid", "start_time"}:
        data["classification"]["groups"]["latency_sensitive"][0][change] += 1
    elif change == "pressure":
        data["snapshot"]["pressure"] = {"cpu": {"some": {"avg10": 30}}}
    elif change == "parameters":
        decision.parameters["cpu_weight"] = 2000
    elif change == "scope":
        data["scope_pids"].append(99)
    elif change == "measurement":
        data["canary_config"]["url"] += "health"
    else:
        generation = 1
    assert trial_context_key(decision, data, generation) != before


def test_unknown_process_identity_cannot_suppress_trials():
    data = context_data()
    data["classification"]["groups"]["latency_sensitive"][0].pop("start_time")
    assert trial_context_key(Decision("latency_first", "nginx"), data, 0) is None


@pytest.fixture
def loop_fixture(tmp_path, monkeypatch):
    data = context_data()
    loop = AgentLoop(AgentContext(state_dir=tmp_path, data=data))
    calls = []
    state = {"scheduler": "disabled", "rollback_ok": True, "errors": 0, "background": False}
    monkeypatch.setattr("schedx.agent.loop.ScxController.state", lambda _: state["scheduler"])
    monkeypatch.setattr("schedx.agent.loop.sched_ext_rejected", lambda: 0)

    def execute(phase, iteration):
        calls.append(phase)
        if phase == "policy":
            loop.context.data["actions"] = [{"target_type": "pid", "target": "42"}]
        elif phase == "canary_baseline":
            loop.context.data["canary"] = {"baseline": metrics(errors=state["errors"]),
                                           "background_expected": state["background"]}
        elif phase == "verify":
            loop.context.data["canary_verdict"] = {"status": "rejected", "reasons": ["p99_regression"]}
            return SkillResult(False, "regression")
        elif phase == "rollback":
            return SkillResult(state["rollback_ok"], "restored" if state["rollback_ok"] else "failed")
        return SkillResult(True, "fixture phase")

    monkeypatch.setattr(loop, "_execute_skill", execute)
    return loop, calls, state


def test_safe_rejection_pauses_identical_mutations_but_keeps_real_measurement(loop_fixture):
    loop, calls, _ = loop_fixture
    assert loop._run_one_round(1)["status"] == "failed_rolled_back"
    for index in range(2, 5):
        calls.clear()
        row = loop._run_one_round(index)
        assert calls == ["probe", "analyze", "canary_baseline"]
        assert row["objective_status"] == "observed_healthy" and row["noop"]
        assert row["improvement"] is None
        assert "canary_verdict" not in loop.context.data
        assert loop.context.data["policy_observation"]["verdict"]["status"] == "accepted"
    calls.clear()
    assert loop._run_one_round(5)["status"] == "failed_rolled_back"
    assert "act" in calls
    assert loop._rejected_trial["observation_rounds"] == 6
    assert loop._rejected_trial["until_round"] == 11


def test_repeated_rejections_have_bounded_backoff_and_new_context_starts_fresh(loop_fixture):
    loop, _, _ = loop_fixture
    for index, pause in ((1, 3), (5, 6), (12, 12), (25, 24), (50, 24)):
        assert loop._run_one_round(index)["status"] == "failed_rolled_back"
        assert loop._rejected_trial["observation_rounds"] == pause
    loop.context.data["classification"]["groups"]["latency_sensitive"][0]["start_time"] += 1
    loop._run_one_round(51)
    assert loop._rejected_trial["attempts"] == 1
    assert loop._rejected_trial["observation_rounds"] == 3


@pytest.mark.parametrize("change", ["pid_identity", "pressure", "scheduler"])
def test_context_or_backend_change_bypasses_the_pause(loop_fixture, change):
    loop, calls, state = loop_fixture
    loop._run_one_round(1)
    if change == "pid_identity":
        loop.context.data["classification"]["groups"]["latency_sensitive"][0]["start_time"] += 1
    elif change == "pressure":
        loop.context.data["snapshot"]["pressure"] = {"cpu": {"some": {"avg10": 30}}}
    else:
        state["scheduler"] = "enabled"
    calls.clear()
    loop._run_one_round(2)
    assert "act" in calls


def test_failed_restoration_never_creates_a_pause(loop_fixture):
    loop, _, state = loop_fixture
    state["rollback_ok"] = False
    assert loop._run_one_round(1)["status"] == "rollback_failed"
    assert loop._rejected_trial is None
    assert loop._run_one_round(2)["status"] == "rollback_failed"


@pytest.mark.parametrize("fault", ["http_error", "missing_background_progress"])
def test_observation_keeps_request_and_background_safety_gates(loop_fixture, fault):
    loop, calls, state = loop_fixture
    loop._run_one_round(1)
    state["errors" if fault == "http_error" else "background"] = 1
    calls.clear()
    row = loop._run_one_round(2)
    assert row["status"] == "observation_failed"
    assert row["observation_verified"] is False
    assert "act" not in calls
    assert loop.context.data["policy_observation"]["verdict"]["status"] == "rejected"


def test_stability_accepts_only_verified_no_action_observations(loop_fixture):
    loop, _, _ = loop_fixture
    loop._run_one_round(1)
    rows = [loop._run_one_round(index) for index in range(2, 5)]
    assert DecisionEngine().assess_stability(rows)["stable"]
    forged = copy.deepcopy(rows)
    forged[0].pop("observation_verified")
    assert not DecisionEngine().assess_stability(forged)["stable"]
    rows[-1]["metrics"]["p99_ms"] = 2
    assert not DecisionEngine().assess_stability(rows)["stable"]


def test_healthy_hard_quota_never_recommends_removing_the_requested_cap():
    measured = {"cpu_stat": {"nr_throttled": 20}}
    assert recommend_next_hint("test", measured, 0, hard_cpu_limit=True) == ""
    assert "preserve the limit" in ToolCallRunner._feedback(measured, 0, hard_cpu_limit=True)[0]
    measured["memory_events"] = {"oom_kill": 1}
    hint = recommend_next_hint("test", measured, 1, hard_cpu_limit=True)
    assert "memory:high" in hint and "cpu:high" not in hint


@pytest.mark.parametrize("queued", [True, False])
def test_standalone_feedback_distinguishes_starved_background_from_absent_work(queued, monkeypatch):
    controller = ScxController(dry_run=False)
    values = iter(({1: {"runtime_ns": 100}, 3: {"runtime_ns": 0, "enqueues": 3 if queued else 0, "runs": 0}},
                   {1: {"runtime_ns": 200}, 3: {"runtime_ns": 0, "enqueues": 3 if queued else 0, "runs": 0}}))
    calls = []

    class Stop:
        count = 0
        def wait(self, seconds):
            self.count += 1
            return self.count > 2
        def is_set(self):
            return False

    monkeypatch.setattr(controller, "get_class_metrics", lambda: next(values))
    monkeypatch.setattr(controller, "set_fairness", lambda interval, ordinary: calls.append((interval, ordinary)))
    controller._adapt_runtime(Stop())
    assert calls == ([(32, 32)] if queued else [])
