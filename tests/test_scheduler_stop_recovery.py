import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from schedx.agent.context import AgentContext
from schedx.benchmark.scx_comparison import _stop_scheduler_context
from schedx.controllers.scx_controller import ScxController
from schedx.cpu_backend import CpuBackendBusy, CpuBackendLease
from schedx.skills.rollback_skill import RollbackSkill
from schedx.skills.scx_skill import ScxSkill
from schedx.tool_runner import ToolCallRunner


class TerminationFailure:
    """Inject bounded wait/kill failures without starting a kernel scheduler."""
    pid = 123456

    def __init__(self):
        self.allow_stop = False
        self.exited = False

    def poll(self):
        return 0 if self.exited else None

    def wait(self, timeout=None):
        if not self.exited:
            raise subprocess.TimeoutExpired("termination-fixture", timeout)
        return 0

    def send_signal(self, signal):
        if not self.allow_stop:
            raise PermissionError("injected termination failure")
        self.exited = True

    def kill(self):
        self.send_signal(None)


def failing_controller(tmp_path):
    controller = ScxController(sys_root=tmp_path / "mock-kernel", dry_run=False)
    process = TerminationFailure()
    controller._process = process
    controller._send_command = lambda command: False
    lock = tmp_path / "backend.lock"
    controller._backend_lease = CpuBackendLease(lock, native=True).acquire()
    return controller, process, lock


def test_failed_stop_preserves_handle_lease_and_allows_retry(tmp_path):
    controller, process, lock = failing_controller(tmp_path)
    try:
        assert not controller.stop_scheduler()
        assert controller._process is process
        assert controller.status()["process_running"]
        assert controller.status()["last_stop_error"]
        with pytest.raises(CpuBackendBusy):
            CpuBackendLease(lock, native=False).acquire()
        process.allow_stop = True
        assert controller.stop_scheduler()
        assert controller._process is None
        assert controller._backend_lease is None
        assert not controller.status()["last_stop_error"]
        quota = CpuBackendLease(lock, native=False).acquire()
        quota.release()
    finally:
        process.allow_stop = True
        controller.stop_scheduler()


def test_new_start_cannot_reuse_a_process_waiting_for_stop_recovery(tmp_path, monkeypatch):
    controller, process, _ = failing_controller(tmp_path)
    assert not controller.stop_scheduler()
    monkeypatch.setattr(controller, "is_available", lambda: True)
    monkeypatch.setattr("schedx.controllers.scx_controller.shutil.which", lambda _: sys.executable)
    monkeypatch.setattr("schedx.controllers.scx_controller.subprocess.Popen", lambda *args, **kwargs: pytest.fail("must not start a new process"))
    try:
        with pytest.raises(RuntimeError, match="recovery required"):
            controller.start_scheduler()
        assert controller._process is process
    finally:
        process.allow_stop = True
        controller.stop_scheduler()


def test_stop_failure_retains_empty_lifecycle_journal_for_next_retry(tmp_path):
    class Client:
        stop_calls = 0

        def remove_task_policy(self, pid):
            return True

        def stop_scheduler(self):
            self.stop_calls += 1
            return self.stop_calls > 1

    context = AgentContext(state_dir=tmp_path)
    client = Client()
    context.data["_scx_controller"] = client
    context.scx_rollback_file.write_text(json.dumps({"source": "standalone_scx", "entries": [{"pid": 123, "previous": None}]}))
    first = RollbackSkill()._rollback_scx(context)
    assert any(row["status"] == "failed" for row in first)
    saved = json.loads(context.scx_rollback_file.read_text())
    assert saved["scheduler_stop_pending"] is True
    assert saved["entries"] == []
    second = RollbackSkill()._rollback_scx(context)
    assert not any(row.get("status") == "failed" for row in second)
    assert client.stop_calls == 2
    assert not context.scx_rollback_file.exists()


def test_start_failure_has_lifecycle_ownership_before_any_policy_ack(tmp_path, monkeypatch):
    context = AgentContext(state_dir=tmp_path, data={"mode": "latency_first", "classification": {
        "groups": {"latency_sensitive": [{"pid": 123, "comm": "nginx"}]}}})
    skill = ScxSkill()
    monkeypatch.setattr(skill.controller, "is_available", lambda: True)
    monkeypatch.setattr(skill.controller, "status", lambda: {"process_running": False})
    monkeypatch.setattr("schedx.skills.scx_skill.ScxDaemonClient.is_available", lambda self: False)

    def failed_start(name):
        assert context.data["_scx_controller"] is skill.controller
        assert json.loads(context.scx_rollback_file.read_text())["source"] == "standalone_scx"
        raise RuntimeError("injected startup failure")

    monkeypatch.setattr(skill.controller, "start_scheduler", failed_start)
    assert not skill.run(context).ok
    assert context.scx_rollback_file.exists()


def test_legacy_comparison_does_not_report_failed_stop_as_success():
    class Client:
        def stop_scheduler(self):
            return False

    log = []
    with pytest.raises(RuntimeError, match="cleanup failed"):
        _stop_scheduler_context({"controller": Client()}, log)
    assert log == [{"event": "stop", "scheduler": "scx_agent", "stopped": False}]


def test_common_adapter_retains_exclusive_lease_until_stop_is_confirmed(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/run_final_round_comparison.py"
    spec = importlib.util.spec_from_file_location("final_stop_adapter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Client:
        stopped = False

        def stop_scheduler(self):
            return self.stopped

        def state(self):
            return "disabled"

    session = module.NativeSession(tmp_path / "no-binary-needed", {}, tmp_path)
    client = Client()
    session.controller = client
    lock = tmp_path / "backend.lock"
    session.lease = CpuBackendLease(lock, native=True).acquire()
    try:
        session.close()
        assert session.evidence["scheduler_stopped"] is False
        assert session.lease is not None
        with pytest.raises(CpuBackendBusy):
            CpuBackendLease(lock, native=False).acquire()
    finally:
        client.stopped = True
        session.close()
    quota = CpuBackendLease(lock, native=False).acquire()
    quota.release()


def test_tool_command_remains_gated_when_failed_start_cannot_stop(tmp_path, monkeypatch):
    marker = tmp_path / "executed"

    class Client:
        _process = object()

        def __init__(self, **kwargs):
            pass

        def is_available(self):
            return True

        def start_scheduler(self):
            raise RuntimeError("injected attach failure")

        def stop_scheduler(self):
            return False

    class Runner(ToolCallRunner):
        def _prepare_group(self, parent, tool, profile):
            tool.mkdir(parents=True)
            (tool / "cgroup.procs").write_text("")

        @staticmethod
        def _cleanup(tool, parent):
            (tool / "cgroup.procs").unlink(missing_ok=True)
            return ToolCallRunner._cleanup(tool, parent)

    monkeypatch.setattr("schedx.tool_runner.ScxController", Client)
    monkeypatch.setattr("schedx.tool_runner.ScxDaemonClient.is_available", lambda self: False)
    runner = Runner(root=tmp_path / "groups", state_dir=tmp_path / "state")
    with pytest.raises(RuntimeError, match="before tool execution"):
        runner.run([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"], intent="interactive")
    assert not marker.exists()
    assert not list((tmp_path / "groups").rglob("tool-*"))


def test_daemon_exit_surfaces_unterminated_scheduler(tmp_path, monkeypatch):
    from schedx.scx_daemon import serve_scx_daemon

    class Client:
        def start_scheduler(self):
            return True

        def stop_scheduler(self):
            return False

    class Server:
        def __init__(self, socket_path, controller):
            socket_path.touch()

        def serve_forever(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr("schedx.scx_daemon._ScxDaemonServer", Server)
    with pytest.raises(RuntimeError, match="could not be terminated"):
        serve_scx_daemon(tmp_path / "daemon.sock", Client())
