from schedx.tool_runner import (
    PROFILES,
    ToolCallRunner,
    infer_intent,
    parse_resource_hint,
    recommend_next_hint,
)


def test_infer_tool_intent():
    assert infer_intent(["pytest", "-q"]) == "test"
    assert infer_intent(["make", "-j4"]) == "compile"
    assert infer_intent(["pip", "install", "x"]) == "package"
    assert infer_intent(["stress-ng", "--cpu", "4"]) == "background"
    assert infer_intent(["git", "status"]) == "interactive"


def test_feedback_reports_pressure():
    feedback = ToolCallRunner._feedback(
        {
            "memory_events": {"high": 2, "oom": 0},
            "cpu_stat": {"nr_throttled": 3},
        },
        1,
    )
    assert len(feedback) == 3


def test_profiles_cover_agent_tool_types():
    assert {"interactive", "test", "compile", "package", "background"} <= set(PROFILES)


def test_parse_resource_hint():
    intent, overrides = parse_resource_hint("intent:compile,memory:high,cpu:low")
    assert intent == "compile"
    assert overrides["memory_max"] == "4G"
    assert overrides["cpu_weight"] == 50


def test_recommend_next_hint_closes_pressure_loop():
    hint = recommend_next_hint(
        "test",
        {
            "memory_events": {"high": 2, "oom": 0},
            "cpu_stat": {"nr_throttled": 3},
        },
        1,
    )
    assert hint == "intent:test,memory:high,cpu:high"


def test_recommend_next_hint_skips_healthy_run():
    assert recommend_next_hint("interactive", {}, 0) == ""
