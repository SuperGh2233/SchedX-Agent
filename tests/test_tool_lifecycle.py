import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from schedx.controllers.scx_controller import ScxController, SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL
from schedx.tool_runner import ToolCallRunner


class FilesystemRunner(ToolCallRunner):
    """Exercise real launcher/IPC/output behavior without kernel resource changes."""

    def _prepare_group(self, parent, tool, profile):
        tool.mkdir(parents=True)
        (tool / "cgroup.procs").write_text("")

    @staticmethod
    def _cleanup(tool, parent):
        (tool / "cgroup.procs").unlink(missing_ok=True)
        return ToolCallRunner._cleanup(tool, parent)


def test_short_tool_starts_after_cgroup_handshake_and_is_cleaned(tmp_path):
    runner = FilesystemRunner(root=tmp_path / "groups", state_dir=tmp_path / "state", native_scx=False)
    result = runner.run([sys.executable, "-c", "print('ready')"])
    assert result["returncode"] == 0
    assert result["stdout"] == "ready\n"
    assert result["cleanup"]["cgroup_removed"]


def test_tool_timeout_stops_process_group(tmp_path):
    runner = FilesystemRunner(root=tmp_path / "groups", state_dir=tmp_path / "state", native_scx=False)
    start = time.monotonic()
    result = runner.run([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5)
    assert result["returncode"] == 124
    assert result["timed_out"]
    assert time.monotonic() - start < 5
    assert result["cleanup"]["cgroup_removed"]


def test_output_is_drained_with_bounded_storage(tmp_path):
    runner = FilesystemRunner(root=tmp_path / "groups", state_dir=tmp_path / "state", native_scx=False)
    result = runner.run([sys.executable, "-c", "import sys; sys.stdout.write('x'*200000); sys.stderr.write('y'*200000)"], output_limit=8192)
    assert result["returncode"] == 0
    assert len(result["stdout"]) == 8192
    assert len(result["stderr"]) == 8192
    assert result["stdout_truncated"] and result["stderr_truncated"]
    assert Path(result["stdout_path"]).stat().st_size == 8192


def test_four_concurrent_tools_have_distinct_groups_and_outputs(tmp_path):
    runner = FilesystemRunner(root=tmp_path / "groups", state_dir=tmp_path / "state", native_scx=False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: runner.run([sys.executable, "-c", f"print({n})"]), range(4)))
    assert len({row["run_id"] for row in results}) == 4
    assert all(row["returncode"] == 0 and row["cleanup"]["cgroup_removed"] for row in results)
    assert {row["stdout"].strip() for row in results} == {"0", "1", "2", "3"}


def test_unsafe_agent_component_cannot_escape_subtree():
    assert ToolCallRunner._safe_name("..") == "default"


def test_ordinary_queue_floor_cannot_be_disabled():
    assert SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL > 0
    with pytest.raises(ValueError, match="default_interval"):
        ScxController().set_fairness(64, 0)


def test_scheduler_read_deadline_reaps_unresponsive_child():
    ctl = ScxController(command_timeout=0.05)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.PIPE)
    ctl._process = child
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            ctl._read_until_prompt()
        assert child.poll() is not None
        assert ctl._process is None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()


def test_standalone_fairness_uses_runtime_feedback(monkeypatch):
    ctl = ScxController(background_interval=64)
    frames = iter(({1: {"runtime_ns": 0}, 3: {"runtime_ns": 0}},
                   {1: {"runtime_ns": 1000}, 3: {"runtime_ns": 10}}))
    monkeypatch.setattr(ctl, "get_class_metrics", lambda: next(frames))
    updates = []

    def update(background, default):
        updates.append((background, default))
        ctl.background_interval = background
        return True

    monkeypatch.setattr(ctl, "set_fairness", update)

    class Stop:
        calls = 0

        def wait(self, seconds):
            self.calls += 1
            return self.calls >= 3

        def is_set(self):
            return False

    ctl._adapt_runtime(Stop())
    assert updates == [(32, SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL)]
    assert ctl._adaptive_report["reason"] == "runtime_share_low"
