import json
from pathlib import Path

from schedx.agent.context import AgentContext
from schedx.skills.act_skill import ActSkill
from schedx.skills.verify_skill import VerifySkill
from schedx.skills.ebpf_skill import (
    EbpfAttachSkill,
    EbpfCleanupSkill,
    EbpfLoadSkill,
    EbpfPolicySkill,
    EbpfStatsSkill,
)
from schedx.skills.rollback_skill import RollbackSkill
from schedx.controllers.cgroup_controller import RollbackEntry
from schedx.controllers.ebpf_controller import EbpfController, EbpfProgType
from schedx.agent.loop import AgentLoop
from schedx.probes.ebpf_probe import EbpfProbe
from schedx.agent.skill import SkillResult


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


def test_agent_applies_ebpf_policy_after_cgroup_actions():
    phases = AgentLoop.PHASES

    assert phases.index("act") < phases.index("ebpf_policy")
    assert phases.index("ebpf_policy") < phases.index("canary_candidate")
    assert phases.index("canary_candidate") < phases.index("ebpf_stats")


def test_ebpf_policy_updates_network_resource_and_security(tmp_path):
    executable = tmp_path / "123" / "exe"
    executable.parent.mkdir()
    executable.write_text("binary", encoding="utf-8")
    calls = []

    class Controller:
        proc_root = tmp_path
        dry_run = False
        last_error = ""

        @staticmethod
        def cgroup_id_for_pid(pid):
            return 77

        @staticmethod
        def is_program_loaded(prog_type):
            return prog_type in {
                EbpfProgType.NET_POLICY,
                EbpfProgType.RESOURCE_CTRL,
                EbpfProgType.SECURITY_POLICY,
            }

        @staticmethod
        def update_net_cgroup_policy(cgroup_id, class_id):
            calls.append(("network", cgroup_id, class_id))
            return True

        @staticmethod
        def update_resource_policy(cgroup_id, class_id):
            calls.append(("resource", cgroup_id, class_id))
            return True

        @staticmethod
        def update_security_policy(cgroup_id, path, **policy):
            calls.append(("security", cgroup_id, Path(path).name, policy))
            return True

    classification = {
        "groups": {
            "background_noise": [{"pid": 123}],
            "unknown": [],
        }
    }
    results = EbpfPolicySkill()._apply_policies(classification, Controller())

    assert results[0]["status"] == "applied"
    assert results[0]["net_policy"] == "applied"
    assert results[0]["resource_policy"] == "applied"
    assert results[0]["security_policy"] == "audit"
    assert [call[0] for call in calls] == ["network", "resource", "security"]
    assert calls[-1][-1] == {"deny_exec": False, "audit_only": True}


def test_ebpf_stats_reuses_active_probe(monkeypatch):
    context = AgentContext(dry_run=False)
    probe = EbpfProbe(dry_run=False)
    snapshot = {"hooks": {"network_policy": {"total_packets": 3}}}
    monkeypatch.setattr(probe, "snapshot", lambda: snapshot)
    context.data["_ebpf_probe"] = probe

    result = EbpfStatsSkill().run(context)

    assert result.ok
    assert context.data["ebpf_stats"] is snapshot


def test_ebpf_cleanup_uses_active_probe(monkeypatch):
    context = AgentContext(dry_run=False)
    probe = EbpfProbe(dry_run=False)
    monkeypatch.setattr(probe, "unload_all", lambda: {"network-policy": True})
    context.data["_ebpf_probe"] = probe

    result = EbpfCleanupSkill().run(context)

    assert result.ok
    assert context.data["ebpf_cleanup"] == {"network-policy": True}
    assert "_ebpf_probe" not in context.data


def test_rollback_includes_ebpf_cleanup(monkeypatch):
    monkeypatch.setattr(
        "schedx.controllers.cgroup_controller.CgroupController.rollback",
        lambda self: [],
    )
    monkeypatch.setattr(
        EbpfCleanupSkill,
        "run",
        lambda self, context: SkillResult(True, "clean", {"results": {"sched_trace": True}}),
    )

    result = RollbackSkill().run(AgentContext(dry_run=False))

    assert result.ok
    assert result.data["ebpf_cleanup"] == {"sched_trace": True}
