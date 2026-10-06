from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import threading
import time

from schedx.tool_runner import ToolCallRunner


class FilesystemRunner(ToolCallRunner):
    def _prepare_group(self, parent, tool, profile):
        tool.mkdir(parents=True)
        (tool / 'cgroup.procs').write_text('')

    @staticmethod
    def _cleanup(tool, parent):
        (tool / 'cgroup.procs').unlink(missing_ok=True)
        return ToolCallRunner._cleanup(tool, parent)


def wait_file(path):
    deadline = time.monotonic() + 3
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.005)
    assert path.exists()


def test_finishing_tool_cannot_remove_shared_parent_during_another_creation(tmp_path):
    entered = threading.Event()
    resume = threading.Event()
    root = tmp_path / 'groups'
    old_started, old_finish, old_exited = [tmp_path / name for name in ('started', 'finish', 'exited')]

    class PausedRunner(FilesystemRunner):
        def _prepare_group(self, parent, tool, profile):
            parent.mkdir(parents=True, exist_ok=True)
            entered.set()
            assert resume.wait(timeout=3)
            # This is the exact directory gap hit by the Linux soak failure.
            tool.mkdir()
            (tool / 'cgroup.procs').write_text('')

    old = FilesystemRunner(root=root, state_dir=tmp_path / 'old-output', native_scx=False)
    new = PausedRunner(root=root, state_dir=tmp_path / 'different-output', native_scx=False)
    code = f"from pathlib import Path; import time\nPath({str(old_started)!r}).touch()\nwhile not Path({str(old_finish)!r}).exists(): time.sleep(.005)\nPath({str(old_exited)!r}).touch()\n"
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(old.run, [sys.executable, '-c', code], agent_id='shared')
        wait_file(old_started)
        second = pool.submit(new.run, [sys.executable, '-c', "print('new')"], agent_id='shared')
        try:
            assert entered.wait(timeout=3)
            old_finish.touch()
            wait_file(old_exited)
            time.sleep(.1)
            assert not first.done(), 'cleanup bypassed the in-progress hierarchy creation'
        finally:
            resume.set()
        for future in (first, second):
            result = future.result(timeout=3)
            assert result['returncode'] == 0 and result['cleanup']['cgroup_removed']
    assert not list(root.rglob('tool-*'))


def test_group_guard_coordinates_independent_processes_and_different_output_directories(tmp_path):
    root = tmp_path / 'groups'
    code = """
from pathlib import Path
import sys,time
from schedx.tool_runner import ToolCallRunner,ToolGroupBusy
try:
 with ToolCallRunner._group_mutation_lock(Path(sys.argv[1]),deadline=time.monotonic()+.08):
  raise SystemExit('unexpected concurrent topology access')
except ToolGroupBusy:
 print('bounded contention')
"""
    with ToolCallRunner._group_mutation_lock(root):
        result = subprocess.run([sys.executable, '-c', code, str(root)], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'bounded contention'
