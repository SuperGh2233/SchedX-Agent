import pytest

from schedx.skills.rollback_skill import RollbackSkill


def process(root, pid, start):
    path = root / str(pid)
    path.mkdir()
    fields = ["0"] * 22
    fields[0] = "S"
    fields[19] = str(start)
    (path / "stat").write_text(f"{pid} (worker) " + " ".join(fields))


def test_exited_and_reused_accepted_tasks_are_not_reapplied(tmp_path):
    process(tmp_path, 11, 101)
    process(tmp_path, 22, 999)
    classification = {"overall": "mixed", "groups": {"latency_sensitive": [{"pid": 11, "start_time": 101}],
                         "background_noise": [{"pid": 22, "start_time": 202}, {"pid": 33, "start_time": 303}]}}
    live, expired = RollbackSkill._live_accepted(classification, tmp_path)
    assert live["groups"]["latency_sensitive"] == [{"pid": 11, "start_time": 101}]
    assert live["groups"]["background_noise"] == []
    assert expired == {22, 33}
    assert len(classification["groups"]["background_noise"]) == 2


def test_unreadable_live_identity_is_not_reported_as_exited(tmp_path):
    (tmp_path / "11").mkdir()
    with pytest.raises(RuntimeError, match="cannot verify identity"):
        RollbackSkill._live_accepted({"groups": {"latency_sensitive": [{"pid": 11, "start_time": 101}]}}, tmp_path)
