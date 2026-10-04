import sys

import pytest

from schedx.controllers.scx_controller import ScxController
from schedx.main import build_parser
from schedx.tool_runner import CpuQuotaUnavailable, ToolCallRunner


class QuotaFilesystemRunner(ToolCallRunner):
    """Real child handshake with simulated cgroup quota files."""

    def _prepare_group(self, parent, tool, profile):
        tool.mkdir(parents=True)
        (tool / "cgroup.procs").write_text("")
        (tool / "cpu.max").write_text(str(profile["cpu_max"]))

    @staticmethod
    def _cleanup(tool, parent):
        for name in ("cpu.max", "cgroup.procs"):
            (tool / name).unlink(missing_ok=True)
        return ToolCallRunner._cleanup(tool, parent)


@pytest.mark.parametrize("quota", ["", "10000", "-1 100000", "0 100000", "100 0", "nan 100000", "max x", "100 100 extra"])
def test_invalid_quota_is_rejected_before_launch(quota):
    with pytest.raises(ValueError):
        ToolCallRunner._quota_parts(quota)


def test_hard_quota_uses_cgroup_without_starting_native_scheduler(monkeypatch, tmp_path):
    monkeypatch.setattr(ScxController, "state", lambda self: "disabled")
    monkeypatch.setattr(ScxController, "cpu_control_support", lambda self: {"cpu_max": "cgroup_v2"})
    monkeypatch.setattr(ScxController, "start_scheduler", lambda *args: pytest.fail("must not start native scheduler for a hard quota"))
    runner = QuotaFilesystemRunner(root=tmp_path / "groups", state_dir=tmp_path / "state")
    result = runner.run([sys.executable, "-c", "print('quota-child')"], profile_overrides={"cpu_max": "10000 100000"})
    assert result["stdout"] == "quota-child\n"
    assert result["hard_cpu_quota_requested"] is True
    assert result["scx_mode"] == "cgroup"
    assert result["cpu_control_support_at_launch"]["cpu_max"] == "cgroup_v2"
    assert result["cleanup"]["cgroup_removed"]


def test_active_native_scheduler_rejects_hard_quota_without_starting_command_or_stopping_owner(monkeypatch, tmp_path):
    marker = tmp_path / "executed"
    monkeypatch.setattr(ScxController, "state", lambda self: "enabled")
    monkeypatch.setattr(ScxController, "stop_scheduler", lambda *args: pytest.fail("must not stop somebody else's scheduler"))
    runner = QuotaFilesystemRunner(root=tmp_path / "groups", state_dir=tmp_path / "state", native_scx=False)
    with pytest.raises(CpuQuotaUnavailable):
        runner.run([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"], intent="background")
    assert not marker.exists()
    assert not (tmp_path / "groups").exists()


def test_missing_quota_file_is_rejected_and_empty_group_cleaned(monkeypatch, tmp_path):
    monkeypatch.setattr(ScxController, "state", lambda self: "disabled")
    monkeypatch.setattr(ScxController, "cpu_control_support", lambda self: {"cpu_max": "cgroup_v2"})
    marker = tmp_path / "executed"

    class MissingQuotaRunner(QuotaFilesystemRunner):
        def _prepare_group(self, parent, tool, profile):
            super()._prepare_group(parent, tool, profile)
            (tool / "cpu.max").unlink()

    runner = MissingQuotaRunner(root=tmp_path / "groups", state_dir=tmp_path / "state")
    with pytest.raises(CpuQuotaUnavailable):
        runner.run([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"], intent="background")
    assert not marker.exists()
    assert not list((tmp_path / "groups").rglob("tool-*"))


def test_explicit_soft_mode_allows_execution_and_reports_unenforced_quota(monkeypatch, tmp_path):
    monkeypatch.setattr(ScxController, "state", lambda self: "enabled")
    monkeypatch.setattr(ScxController, "cpu_control_support", lambda self: {"cpu_max": "not_enforced"})
    runner = QuotaFilesystemRunner(root=tmp_path / "groups", state_dir=tmp_path / "state", native_scx=False)
    result = runner.run([sys.executable, "-c", "print('soft-child')"], intent="background", cpu_limit_mode="soft")
    assert result["returncode"] == 0
    assert result["cpu_limit_mode"] == "soft"
    assert not result["hard_cpu_quota_requested"]
    assert any("does not enforce" in line for line in result["feedback"])


def test_scheduler_change_before_gate_keeps_actual_command_blocked(monkeypatch, tmp_path):
    states = iter(("disabled", "disabled", "enabled"))
    monkeypatch.setattr(ScxController, "state", lambda self: next(states))
    monkeypatch.setattr(ScxController, "cpu_control_support", lambda self: {"cpu_max": "cgroup_v2"})
    marker = tmp_path / "executed"
    runner = QuotaFilesystemRunner(root=tmp_path / "groups", state_dir=tmp_path / "state")
    with pytest.raises(CpuQuotaUnavailable):
        runner.run([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"], intent="background")
    assert not marker.exists()
    assert not list((tmp_path / "groups").rglob("tool-*"))


def test_cli_defaults_to_hard_and_allows_explicit_soft_mode():
    parser = build_parser()
    assert parser.parse_args(["tool-run", "--", "true"]).cpu_limit_mode == "hard"
    assert parser.parse_args(["tool-run", "--cpu-limit-mode", "soft", "--", "true"]).cpu_limit_mode == "soft"
