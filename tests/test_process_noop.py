from types import SimpleNamespace

import pytest

from schedx.agent.actions import Action
from schedx.agent.executor import SafeActionExecutor
from schedx.controllers.cgroup_controller import CgroupController


def executor(tmp_path, monkeypatch):
    ctl = CgroupController(root=tmp_path, rollback_file=tmp_path / "rollback.json", dry_run=False, owner="monitor", transaction="candidate")
    runner = SafeActionExecutor(ctl)
    monkeypatch.setattr(runner, "_is_protected", lambda *args: False)
    monkeypatch.setattr(runner.process_state, "record", lambda *args: pytest.fail("unchanged settings must not add recovery entries"))
    monkeypatch.setattr("schedx.agent.executor.subprocess.run", lambda *args, **kwargs: pytest.fail("unchanged settings must not invoke external commands"))
    return runner


def test_repeated_nice_setting_does_not_mutate_or_record(tmp_path, monkeypatch):
    runner = executor(tmp_path, monkeypatch)
    monkeypatch.setattr("schedx.agent.executor.os.getpriority", lambda *args: 5)
    monkeypatch.setattr("schedx.agent.executor.os.setpriority", lambda *args: pytest.fail("no redundant nice change"))
    for _ in range(4):
        result = runner.execute([Action("set_nice", "123", "pid", 5)])
        assert result[0]["status"] == "ok" and result[0]["unchanged"]
    assert not runner.process_state.path.exists()


def test_repeated_scheduling_policy_does_not_mutate_or_record(tmp_path, monkeypatch):
    runner = executor(tmp_path, monkeypatch)
    monkeypatch.setattr("schedx.agent.executor.os.SCHED_BATCH", 3, raising=False)
    monkeypatch.setattr("schedx.agent.executor.os.sched_getscheduler", lambda _: 3, raising=False)
    monkeypatch.setattr("schedx.agent.executor.os.sched_getparam", lambda _: SimpleNamespace(sched_priority=0), raising=False)
    result = runner.execute([Action("set_sched_policy", "123", "pid", "SCHED_BATCH")])
    assert result[0]["status"] == "ok" and result[0]["unchanged"]


@pytest.mark.parametrize("mask", ["0-2", "2,0,1", "0,1-2"])
def test_equivalent_affinity_masks_do_not_mutate_or_record(tmp_path, monkeypatch, mask):
    runner = executor(tmp_path, monkeypatch)
    monkeypatch.setattr("schedx.agent.executor.os.sched_getaffinity", lambda _: {0, 1, 2}, raising=False)
    result = runner.execute([Action("set_affinity", "123", "pid", mask)])
    assert result[0]["status"] == "ok" and result[0]["unchanged"]


@pytest.mark.parametrize("mask", ["0,bad", "0-999999999", "2-0", "0:2", ""])
def test_invalid_or_different_masks_do_not_bypass_command_validation(mask):
    assert not SafeActionExecutor._affinity_matches(mask, {0, 1, 2})
