import pytest

from schedx.agent.decision import Decision
from schedx.llm.client import LLMError
from schedx.llm.policy_planner import LLMPolicyPlanner


def fallback():
    return Decision("balanced", "nginx", {"cpu_weight": 500}, "safe baseline", 0.8)


def classification():
    return {"groups": {"latency_sensitive": [{"comm": "nginx"}], "background_noise": [{"comm": "stress-ng"}]}}


def test_llm_policy_validator_accepts_bounded_proposal():
    result = LLMPolicyPlanner().validate(
        {
            "mode": "latency_first",
            "target": "nginx",
            "parameters": {"cpu_weight": 9000, "cpu_weight_bg": 50, "unknown": 1},
            "reason": "protect request latency",
            "confidence": 0.9,
        },
        classification(),
        fallback(),
    )
    assert result.mode == "latency_first"
    assert result.parameters["cpu_weight"] == 9000
    assert "unknown" not in result.parameters


def test_llm_policy_validator_rejects_unsafe_value():
    with pytest.raises(LLMError):
        LLMPolicyPlanner().validate(
            {
                "mode": "latency_first",
                "target": "nginx",
                "parameters": {"cpu_weight": 99999},
            },
            classification(),
            fallback(),
        )


def test_llm_policy_validator_replaces_unknown_target():
    result = LLMPolicyPlanner().validate(
        {"mode": "balanced", "target": "systemd", "parameters": {}},
        classification(),
        fallback(),
    )
    assert result.target == "nginx"
