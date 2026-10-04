import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from schedx.agent.actions import Action
from schedx.agent.context import AgentContext
from schedx.agent.decision import DecisionEngine
from schedx.agent.executor import SafeActionExecutor
from schedx.agent.loop import AgentLoop
from schedx.agent.skill import SkillResult
from schedx.controllers.cgroup_controller import CgroupController, RollbackEntry
from schedx.skills.act_skill import ActSkill
from schedx.skills.rollback_skill import RollbackSkill
from schedx.skills.scx_skill import ScxSkill
from schedx.skills.ebpf_skill import EbpfCleanupSkill


def test_failed_restoration_is_retained_and_retry_survives_restart(tmp_path, monkeypatch):
    target = tmp_path / "cpu.weight"
    target.write_text("50")
    journal = tmp_path / "rollback.json"
    journal.write_text(json.dumps([{"path": str(tmp_path), "file": "cpu.weight", "previous": "100"}]))
    original = Path.write_text

    def fail(path, *args, **kwargs):
        if path == target:
            raise PermissionError("temporary failure")
        return original(path, *args, **kwargs)

    ctl = CgroupController(root=tmp_path / "cgroup", rollback_file=journal, dry_run=False)
    with monkeypatch.context() as changes:
        changes.setattr(Path, "write_text", fail)
        result = ctl.rollback()
    assert result[0]["status"] == "failed"
    assert journal.exists()
    restarted = CgroupController(root=ctl.root, rollback_file=journal, dry_run=False)
    restarted.rollback()
    assert target.read_text() == "100"
    assert not journal.exists()


def test_failed_latest_restore_does_not_apply_earlier_value(tmp_path, monkeypatch):
    target = tmp_path / "cpu.weight"
    journal = tmp_path / "rollback.json"
    journal.write_text(json.dumps([
        {"path": str(tmp_path), "file": "cpu.weight", "previous": "100"},
        {"path": str(tmp_path), "file": "cpu.weight", "previous": "50"},
    ]))
    writes = []
    original = Path.write_text

    def fail(path, value, **kwargs):
        if path == target:
            writes.append(value)
            raise PermissionError("failure")
        return original(path, value, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail)
    CgroupController(root=tmp_path / "cgroup", rollback_file=journal, dry_run=False).rollback()
    assert writes == ["50"]
    assert len(json.loads(journal.read_text())) == 2


def test_four_concurrent_journal_writers_do_not_lose_records(tmp_path):
    journal = tmp_path / "rollback.json"

    def append(i):
        CgroupController(rollback_file=journal, dry_run=False, owner=str(i))._append_rollback(
            RollbackEntry(str(tmp_path / str(i)), "cpu.weight", "100", str(i))
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(40)))
    assert len(json.loads(journal.read_text())) == 40


def test_candidate_rollback_preserves_previous_accepted_setting(tmp_path):
    target = tmp_path / "cpu.weight"
    target.write_text("25")
    journal = tmp_path / "rollback.json"
    ctl = CgroupController(root=tmp_path / "cgroup", rollback_file=journal, dry_run=False, owner="agent", transaction="second")
    ctl._append_rollback(RollbackEntry(str(tmp_path), "cpu.weight", "100", "agent", transaction="first"))
    ctl._append_rollback(RollbackEntry(str(tmp_path), "cpu.weight", "50", "agent", transaction="second"))
    ctl.rollback()
    assert target.read_text() == "50"
    assert [entry.transaction for entry in ctl._load_rollback()] == ["first"]
    CgroupController(root=ctl.root, rollback_file=journal, dry_run=False).rollback()
    assert target.read_text() == "100"


def test_resource_owner_conflict_is_rejected(tmp_path):
    journal = tmp_path / "rollback.json"
    first = CgroupController(rollback_file=journal, dry_run=False, owner="first")
    first._append_rollback(RollbackEntry("/group", "cpu.weight", "100", "first"))
    second = CgroupController(rollback_file=journal, dry_run=False, owner="second")
    with pytest.raises(RuntimeError, match="owned"):
        second._append_rollback(RollbackEntry("/group", "cpu.weight", "50", "second"))


def test_different_pids_can_share_original_cgroup_without_ownership_conflict(tmp_path):
    journal = tmp_path / "rollback.json"
    for pid in (123, 456):
        ctl = CgroupController(rollback_file=journal, dry_run=False, owner=str(pid))
        ctl._append_rollback(RollbackEntry("/original", "cgroup.procs", str(pid), str(pid)))
    assert len(json.loads(journal.read_text())) == 2


def test_rollback_does_not_remove_other_owners_empty_group(tmp_path):
    root = tmp_path / "groups"
    own = root / "schedx" / "pid-123"
    peer = root / "schedx" / "pid-456"
    own.mkdir(parents=True)
    peer.mkdir()
    ctl = CgroupController(root=root, rollback_file=tmp_path / "rollback.json", owner="own", dry_run=False)
    ctl._created_groups.add(own)
    ctl.rollback()
    assert not own.exists()
    assert peer.exists()


def test_unchanged_control_does_not_grow_journal(tmp_path, monkeypatch):
    target = tmp_path / "cpu.weight"
    target.write_text("50")
    ctl = CgroupController(rollback_file=tmp_path / "rollback.json", dry_run=False)
    monkeypatch.setattr(ctl, "validate_writable", lambda: None)
    ctl._write(target, "50")
    assert not ctl.rollback_file.exists()


def test_unreadable_original_setting_is_not_overwritten(tmp_path, monkeypatch):
    target = tmp_path / "cpu.weight"
    target.write_text("100")
    ctl = CgroupController(rollback_file=tmp_path / "rollback.json", dry_run=False)
    monkeypatch.setattr(ctl, "validate_writable", lambda: None)
    original = Path.read_text

    def deny(path, *args, **kwargs):
        if path == target:
            raise PermissionError("cannot record original value")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as changes:
        changes.setattr(Path, "read_text", deny)
        with pytest.raises(PermissionError):
            ctl._write(target, "50")
    assert target.read_text() == "100"


def test_failed_preview_never_rolls_back_live_state(tmp_path, monkeypatch):
    ctl = CgroupController(rollback_file=tmp_path / "rollback.json", dry_run=False)
    executor = SafeActionExecutor(ctl)
    restored = []
    monkeypatch.setattr(ctl, "rollback", lambda: restored.append("cgroup") or [])
    monkeypatch.setattr(executor.process_state, "rollback", lambda: restored.append("process") or [])
    results = executor.execute([Action("unknown", "123", "pid", 1)], dry_run=True)
    assert not restored
    assert results[-1]["status"] == "failed"


def test_process_recovery_keeps_failed_latest_and_earlier_entries(tmp_path, monkeypatch):
    from schedx.controllers.process_state import ProcessState
    journal = tmp_path / "process.json"
    rows = [{"pid": 123, "pid_start": "start", "kind": "nice", "previous": value} for value in (0, 5)]
    journal.write_text(json.dumps(rows))
    writes = []
    monkeypatch.setattr("schedx.controllers.process_state.process_start_time", lambda pid: "start")

    def fail(which, pid, value):
        writes.append(value)
        raise PermissionError("temporary failure")

    monkeypatch.setattr("schedx.controllers.process_state.os.setpriority", fail)
    assert ProcessState(journal).rollback()[0]["status"] == "failed"
    assert writes == [5]
    assert len(json.loads(journal.read_text())) == 2


def test_scope_isolation_keeps_other_session_hooks(tmp_path, monkeypatch):
    from schedx.skills.ebpf_skill import EbpfLoadSkill
    from schedx.probes.ebpf_probe import EbpfProbe
    context = AgentContext(state_dir=tmp_path)
    probe = EbpfProbe(dry_run=False)
    monkeypatch.setattr("schedx.skills.ebpf_skill._scoped_probe", lambda ctx: probe)
    monkeypatch.setattr(probe, "is_available", lambda: True)
    monkeypatch.setattr(probe.controller, "has_pinned_programs", lambda: True)
    cleaned = []
    monkeypatch.setattr(probe, "cleanup_pinned", lambda: cleaned.append(True))
    (tmp_path / "ebpf_state.json").write_text(json.dumps({"owner": "other"}))
    assert not EbpfLoadSkill().run(context).ok
    assert not cleaned


def test_cli_failure_and_recovery_exit_codes(tmp_path, monkeypatch, capsys):
    from schedx.main import build_parser, cmd_optimize
    args = build_parser().parse_args(["optimize", "--dry-run", "--state-dir", str(tmp_path)])
    for status, expected in (("success", 0), ("degraded", 0), ("rolled_back", 1), ("failed", 1), ("rollback_failed", 3)):
        monkeypatch.setattr(AgentLoop, "run", lambda *args, value=status, **kwargs: {"final_status": value})
        assert cmd_optimize(args) == expected
        json.loads(capsys.readouterr().out)


def test_external_command_failure_stops_and_rolls_back(tmp_path, monkeypatch):
    ctl = CgroupController(rollback_file=tmp_path / "rollback.json", dry_run=False)
    executor = SafeActionExecutor(ctl)
    executor.processes = type("FakeProcesses", (), {})()
    monkeypatch.setattr(executor.process_state, "record", lambda *args: None)
    monkeypatch.setattr(executor.process_state, "rollback", lambda: [])
    monkeypatch.setattr("schedx.agent.executor.os.sched_getscheduler", lambda pid: 0, raising=False)
    monkeypatch.setattr("schedx.agent.executor.os.sched_getparam", lambda pid: type("P", (), {"sched_priority": 0})(), raising=False)
    commands = []

    def fail(command, **kwargs):
        commands.append(command)
        assert kwargs["check"] is True
        raise subprocess.CalledProcessError(1, command, stderr="invalid setting")

    monkeypatch.setattr("schedx.agent.executor.subprocess.run", fail)
    results = executor.execute([Action("set_sched_policy", "123", "pid", "SCHED_BATCH"), Action("set_sched_policy", "456", "pid", "SCHED_BATCH")])
    assert commands == [["chrt", "--batch", "--pid", "0", "123"]]
    assert results[0]["status"] == "failed"
    assert results[-1]["status"] == "failed_rolled_back"


def test_act_skill_propagates_failed_result(tmp_path, monkeypatch):
    monkeypatch.setattr(SafeActionExecutor, "execute", lambda *args, **kwargs: [{"status": "failed"}])
    context = AgentContext(state_dir=tmp_path, data={"actions": [Action("set_affinity", "123", "pid", "0")]})
    assert not ActSkill().run(context).ok


def test_scx_all_failed_is_not_active(tmp_path, monkeypatch):
    class Client:
        def is_available(self):
            return True

        def request(self, action):
            return {"task_policies": {"123": {"class_id": 2, "weight": 1000}}}

        def set_task_policy(self, *args):
            return False

    monkeypatch.setattr("schedx.skills.scx_skill.ScxDaemonClient", Client)
    skill = ScxSkill()
    monkeypatch.setattr(skill.controller, "is_available", lambda: True)
    context = AgentContext(state_dir=tmp_path, data={"classification": {"groups": {"latency_sensitive": [{"pid": 123, "comm": "nginx"}]}}})
    result = skill.run(context)
    assert not result.ok
    assert context.data["scx_status"] == "error"
    assert result.data["failed"] == 1
    assert json.loads(context.scx_rollback_file.read_text())["entries"][0]["previous"]["weight"] == 1000


def test_rollback_skill_does_not_hide_cgroup_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(CgroupController, "rollback", lambda self: [{"status": "failed", "reason": "restore_failed: busy"}])
    monkeypatch.setattr(EbpfCleanupSkill, "run", lambda *args: SkillResult(True, "clean", {"results": {}}))
    assert not RollbackSkill().run(AgentContext(state_dir=tmp_path)).ok


def test_scx_false_restoration_is_retained(tmp_path, monkeypatch):
    class Client:
        def is_available(self):
            return True

        def remove_task_policy(self, pid):
            return False

    monkeypatch.setattr("schedx.skills.rollback_skill.ScxDaemonClient", Client)
    context = AgentContext(state_dir=tmp_path)
    context.scx_rollback_file.write_text(json.dumps({"source": "persistent_scx_daemon", "pids": [123]}))
    results = RollbackSkill()._rollback_scx(context)
    assert results[0]["status"] == "failed"
    assert context.scx_rollback_file.exists()


def test_cgroup_mirror_registers_workers_and_rolls_back(tmp_path, monkeypatch):
    from schedx.skills.scx_skill import ScxCgroupSkill
    path = tmp_path / "groups" / "schedx" / "pid-123"
    path.mkdir(parents=True)
    registered = []

    class Client:
        def is_available(self):
            return True

        def request(self, action):
            return {"cgroup_policies": {}}

        def set_cgroup_policy(self, cgroup_id, class_id, weight):
            registered.append((cgroup_id, class_id, weight))
            return True

        def remove_cgroup_policy(self, cgroup_id):
            registered.remove((cgroup_id, 2, 1000))
            return True

    monkeypatch.setattr("schedx.skills.scx_skill.ScxDaemonClient", Client)
    monkeypatch.setattr("schedx.skills.rollback_skill.ScxDaemonClient", Client)
    context = AgentContext(state_dir=tmp_path / "state", data={
        "scx_status": "active", "cgroup_root": str(tmp_path / "groups"),
        "classification": {"groups": {"batch_compute": [{"pid": 123, "comm": "sysbench"}]}},
        "execution_results": [{"status": "ok", "pid": 123, "group": "pid-123"}],
        "mode": "balanced",
    })
    assert ScxCgroupSkill().run(context).ok
    assert registered == [(path.stat().st_ino, 2, 1000)]
    assert RollbackSkill()._rollback_scx(context)[0]["status"] == "removed"
    assert not registered


def fake_round(loop, failing_phase=None, rollback_ok=True):
    phases = []

    def execute(phase, iteration):
        phases.append(phase)
        if phase == "analyze":
            loop.context.data["classification"] = {"groups": {}}
        return SkillResult(rollback_ok if phase == "rollback" else phase != failing_phase, phase)

    loop._execute_skill = execute
    return phases


def test_failed_plan_never_executes_stale_actions(tmp_path):
    loop = AgentLoop(AgentContext(state_dir=tmp_path, data={"actions": ["stale"]}))
    phases = fake_round(loop, "policy")
    result = loop._run_one_round(1)
    assert result["status"] == "policy_failed"
    assert "act" not in phases
    assert "actions" not in loop.context.data


def test_round_reports_failed_recovery_and_stops_new_mutations(tmp_path):
    loop = AgentLoop(AgentContext(state_dir=tmp_path))
    phases = fake_round(loop, "act", rollback_ok=False)
    assert loop._run_one_round(1)["status"] == "rollback_failed"
    before = len(phases)
    assert loop._run_one_round(2)["status"] == "rollback_failed"
    assert len(phases) == before


def test_continuous_mode_does_not_stop_after_three_healthy_rounds(tmp_path, monkeypatch):
    loop = AgentLoop(AgentContext(state_dir=tmp_path))
    monkeypatch.setattr(loop, "_run_one_round", lambda i: {"status": "ok", "objective_status": "unmeasured"})
    monkeypatch.setattr("schedx.agent.loop.time.sleep", lambda _: None)
    assert loop.run_continuous(interval=0, max_rounds=5) == "max_rounds"
    assert len(loop.round_history) == 5
    assert not loop.context.data["converged"]


def test_three_failed_rounds_are_faults_not_convergence(tmp_path, monkeypatch):
    loop = AgentLoop(AgentContext(state_dir=tmp_path))
    monkeypatch.setattr(loop, "_run_one_round", lambda i: {"status": "probe_failed"})
    monkeypatch.setattr("schedx.agent.loop.time.sleep", lambda _: None)
    assert loop.run_continuous(interval=0) == "consecutive_failures"
    assert not loop.context.data["converged"]


def test_convergence_requires_stable_measured_objectives():
    engine = DecisionEngine()
    row = {"status": "ok", "objective_status": "accepted", "improvement": 0.5, "decision": {"mode": "latency_first", "target": "nginx"}, "metrics": {"p99_ms": 10.0}}
    assert engine.should_stop([row, row, row])
    assert not engine.should_stop([{"status": "probe_failed"}] * 3)
    assert not engine.should_stop([{**row, "improvement": None}] * 3)
    assert not engine.should_stop([row, row, {**row, "decision": {"mode": "balanced"}}])


def test_session_ids_are_unique():
    assert AgentContext().session.session_id != AgentContext().session.session_id


def test_empty_target_never_matches_every_process():
    from schedx.controllers.process_controller import ProcessController
    from schedx.policies.planner import PolicyPlanner
    assert ProcessController().find_by_name("") == []
    assert PolicyPlanner().plan("balanced", "", {"groups": {}}) == []


def test_compatibility_modules_use_canonical_implementation():
    from schedx.loop import AgentLoop as LegacyLoop
    from schedx.scx_controller import ScxController as LegacyController
    from schedx.controllers.scx_controller import ScxController
    assert LegacyLoop is AgentLoop
    assert LegacyController is ScxController
