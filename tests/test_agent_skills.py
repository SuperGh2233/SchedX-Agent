from schedx.agent.context import AgentContext
from schedx.skills.act_skill import ActSkill
from schedx.skills.verify_skill import VerifySkill
from schedx.skills.ebpf_skill import EbpfAttachSkill, EbpfLoadSkill, EbpfPolicySkill
from schedx.skills.rollback_skill import RollbackSkill
from schedx.controllers.cgroup_controller import RollbackEntry
from schedx.controllers.ebpf_controller import EbpfController, EbpfProgType
import json


def test_act_skill_treats_empty_plan_as_safe_noop():
    context = AgentContext(dry_run=True)
    context.data["actions"] = []

    result = ActSkill().run(context)

    assert result.ok
    assert context.data["execution_noop"] is True
    assert context.data["execution_results"] == []


def test_verify_skill_accepts_safe_noop(monkeypatch):
    context = AgentContext(dry_run=True)
    context.data["execution_results"] = []
    context.data["execution_noop"] = True
    monkeypatch.setattr(VerifySkill, "_check_system_state", lambda self: {"cgroup_v2": False})

    result = VerifySkill().run(context)

    assert result.ok
    assert result.message == "verification completed: safe no-op"


def test_ebpf_dry_run_pipeline_preserves_controller_state(monkeypatch):
    context = AgentContext(dry_run=True)
    context.data["classification"] = {
        "groups": {
            "latency_sensitive": [{"pid": 1234}],
            "batch_compute": [],
            "background_noise": [],
            "unknown": [],
        }
    }
    monkeypatch.setattr("schedx.controllers.ebpf_controller.EbpfController.is_available", lambda self: True)

    assert EbpfLoadSkill().run(context).ok
    assert EbpfAttachSkill().run(context).ok
    assert EbpfPolicySkill().run(context).ok
    assert context.data["ebpf_status"] == "attached"


def test_rollback_skill_reports_settings_and_removed_groups(monkeypatch):
    entries = [
        RollbackEntry("/sys/fs/cgroup/schedx/pid-1", "cpu.weight", "100"),
        {"path": "/sys/fs/cgroup/schedx/pid-1", "status": "removed"},
    ]
    monkeypatch.setattr(
        "schedx.controllers.cgroup_controller.CgroupController.rollback",
        lambda self: entries,
    )

    context = AgentContext(dry_run=False)
    result = RollbackSkill().run(context)

    assert result.data["restored"] == 1
    assert result.data["groups_removed"] == 1
    assert context.data["rollback"] == result.data


def test_rollback_skill_removes_persistent_scx_policies(tmp_path, monkeypatch):
    removed = []

    class FakeClient:
        def is_available(self):
            return True

        def remove_task_policy(self, pid):
            removed.append(pid)
            return True

    context = AgentContext(state_dir=tmp_path)
    context.scx_rollback_file.write_text(
        json.dumps({"source": "persistent_scx_daemon", "pids": [10, 20]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("schedx.skills.rollback_skill.ScxDaemonClient", FakeClient)
    monkeypatch.setattr(
        "schedx.controllers.cgroup_controller.CgroupController.rollback",
        lambda self: [],
    )

    result = RollbackSkill().run(context)

    assert result.ok
    assert removed == [10, 20]
    assert not context.scx_rollback_file.exists()
    assert all(entry["status"] == "removed" for entry in result.data["scx_entries"])
    assert context.data["rollback"] == result.data


def test_ebpf_controller_uses_build_output_directory(tmp_path):
    controller = EbpfController(ebpf_dir=tmp_path)

    assert controller._get_object_path(EbpfProgType.SCHED_TRACE) == tmp_path / "build" / "sched_trace.bpf.o"


def test_ebpf_attach_failure_unloads_programs(monkeypatch):
    context = AgentContext(dry_run=False)
    context.data["ebpf_status"] = "loaded"
    from schedx.probes.ebpf_probe import EbpfProbe
    probe = EbpfProbe(dry_run=False)
    monkeypatch.setattr(probe, "attach_all", lambda: {"scheduler-trace": False})
    unloaded = []
    monkeypatch.setattr(probe, "unload_all", lambda: unloaded.append(True))
    context.data["_ebpf_probe"] = probe

    result = EbpfAttachSkill().run(context)

    assert not result.ok
    assert unloaded == [True]
    assert context.data["ebpf_status"] == "unavailable"
