import json
from pathlib import Path

import pytest

from schedx.policies.repository import PolicyRepository


def test_repository_loads_allowlisted_builtin_experts(tmp_path: Path):
    repository = PolicyRepository(tmp_path / "policies.json")

    experts = {expert.expert_id: expert for expert in repository.list_experts()}

    assert set(experts) == {
        "latency_guard",
        "throughput_boost",
        "background_isolation",
        "balanced",
    }
    assert experts["latency_guard"].mode == "latency_first"
    assert experts["throughput_boost"].mode == "throughput_first"


def test_repository_persists_policy_outcomes(tmp_path: Path):
    path = tmp_path / "policies.json"
    repository = PolicyRepository(path)

    repository.record_outcome(
        "latency_guard",
        accepted=True,
        metrics={"p99_delta_percent": -12.5},
        reason="canary improved p99",
    )

    reloaded = PolicyRepository(path)
    outcome = reloaded.outcome_for("latency_guard")
    assert outcome.observations == 1
    assert outcome.accepts == 1
    assert outcome.rejects == 0
    assert outcome.last_metrics == {"p99_delta_percent": -12.5}
    assert outcome.last_reason == "canary improved p99"
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_repository_rejects_unknown_expert(tmp_path: Path):
    repository = PolicyRepository(tmp_path / "policies.json")

    with pytest.raises(KeyError, match="unknown expert"):
        repository.record_outcome("not-allowlisted", accepted=False)


def test_repository_keeps_malformed_file_and_falls_back_to_builtins(tmp_path: Path):
    path = tmp_path / "policies.json"
    path.write_text("{not-json", encoding="utf-8")

    repository = PolicyRepository(path)

    assert len(repository.list_experts()) == 4
    assert path.read_text(encoding="utf-8") == "{not-json"
    assert repository.load_error

