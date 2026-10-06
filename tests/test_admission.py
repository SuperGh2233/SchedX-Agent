import json
import fcntl
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from schedx.admission import AdmissionConfig, AdmissionError, AdmissionTimeout, ToolAdmission
from schedx.tool_runner import ToolCallRunner
from schedx.controllers.scx_controller import ScxController
from schedx.main import build_parser, cmd_tool_run


def controller(tmp_path, **kwargs):
    return ToolAdmission(tmp_path / "admission", AdmissionConfig(mode="fixed", initial_limit=kwargs.pop("limit", 1),
        max_limit=kwargs.pop("max_limit", 1), interactive_reserve=kwargs.pop("reserve", 0), **kwargs),
        cgroup_root=tmp_path / "groups", poll_seconds=0.01)


def acquire(ctl, agent="holder", intent="interactive", timeout=3):
    now = time.monotonic()
    return ctl.acquire(agent, intent, submitted_at=now, deadline=now + timeout)


def wait_pending(ctl, count):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if ctl.snapshot()["pending"] == count:
            return
        time.sleep(0.01)
    pytest.fail("expected callers did not enter the queue")


@pytest.mark.parametrize("values", [
    {"mode": "invalid"}, {"initial_limit": 0}, {"max_limit": True}, {"min_limit": 5},
    {"initial_limit": 9}, {"interactive_reserve": 8}, {"interactive_reserve": -1},
    {"aging_seconds": float("nan")}, {"sample_seconds": float("inf")},
    {"cooldown_seconds": 0}, {"max_pending": False},
])
def test_invalid_configuration_is_rejected(values):
    with pytest.raises(ValueError):
        AdmissionConfig(**values)


def test_interactive_priority_and_aged_background_progress(tmp_path):
    for index, aging, expected in ((0, 10, ["interactive", "background"]),
                                  (1, 0.02, ["background", "interactive"])):
        ctl = controller(tmp_path / str(index), aging_seconds=aging)
        holder = acquire(ctl)
        order = []
        def run(intent):
            lease = acquire(ctl, intent, intent)
            try:
                order.append(intent)
                time.sleep(0.03)
            finally:
                ctl.release(lease)
        with ThreadPoolExecutor(max_workers=2) as pool:
            background = pool.submit(run, "background")
            wait_pending(ctl, 1)
            if aging < 1:
                time.sleep(0.03)
            interactive = pool.submit(run, "interactive")
            wait_pending(ctl, 2)
            ctl.release(holder)
            background.result(timeout=3)
            interactive.result(timeout=3)
        assert order == expected
        assert ctl.snapshot()["active"] == ctl.snapshot()["pending"] == 0


def test_agents_with_more_submissions_do_not_jump_the_same_priority_queue(tmp_path):
    ctl = controller(tmp_path, aging_seconds=10)
    holder = acquire(ctl, "agent-a", "compile")
    order = []
    def run(agent):
        lease = acquire(ctl, agent, "compile")
        order.append(agent)
        ctl.release(lease)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(run, "agent-a")
        wait_pending(ctl, 1)
        second = pool.submit(run, "agent-a")
        wait_pending(ctl, 2)
        third = pool.submit(run, "agent-b")
        wait_pending(ctl, 3)
        ctl.release(holder)
        for future in (first, second, third):
            future.result(timeout=3)
    assert order == ["agent-b", "agent-a", "agent-a"]


def test_reservation_leaves_room_for_an_interactive_submission(tmp_path):
    ctl = controller(tmp_path, limit=2, max_limit=2, reserve=1)
    holder = acquire(ctl, "compile", "compile")
    with ThreadPoolExecutor(max_workers=1) as pool:
        background = pool.submit(acquire, ctl, "background", "background")
        wait_pending(ctl, 1)
        interactive = acquire(ctl, "interactive", "interactive")
        assert ctl.snapshot()["active"] == 2
        assert not background.done()
        ctl.release(interactive)
        assert not background.done()
        ctl.release(holder)
        ctl.release(background.result(timeout=3))


def test_queue_timeout_and_capacity_do_not_leak_slots(tmp_path):
    ctl = controller(tmp_path, max_pending=1)
    holder = acquire(ctl)
    with ThreadPoolExecutor(max_workers=1) as pool:
        queued = pool.submit(acquire, ctl, "waiting", "compile", 0.15)
        wait_pending(ctl, 1)
        with pytest.raises(AdmissionError, match="queue is full"):
            acquire(ctl, "overflow", "interactive")
        with pytest.raises(AdmissionTimeout):
            queued.result(timeout=2)
    assert ctl.snapshot()["pending"] == 0
    assert ctl.snapshot()["active"] == 1
    ctl.release(holder)
    assert not list(ctl.directory.glob("*.lease"))


def test_configuration_conflict_is_rejected_while_a_lease_is_live(tmp_path):
    ctl = controller(tmp_path)
    holder = acquire(ctl)
    other = controller(tmp_path, limit=2, max_limit=2)
    with pytest.raises(AdmissionError, match="different configuration"):
        acquire(other)
    ctl.release(holder)
    lease = acquire(other)
    assert lease.telemetry["limit"] == 2
    other.release(lease)


def test_corrupt_state_is_preserved(tmp_path):
    ctl = controller(tmp_path)
    ctl.directory.mkdir()
    ctl.path.write_text("invalid JSON")
    with pytest.raises(AdmissionError, match="could not be read"):
        acquire(ctl)
    assert ctl.path.read_text() == "invalid JSON"
    assert not list(ctl.directory.glob("*.lease"))


def test_lock_contention_respects_the_submission_deadline(tmp_path):
    ctl = controller(tmp_path)
    ctl.directory.mkdir()
    with ctl.path.with_suffix(".json.lock").open("w") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX)
        start = time.monotonic()
        with pytest.raises(AdmissionTimeout) as failure:
            acquire(ctl, timeout=0.05)
        assert time.monotonic() - start < 0.3
        assert failure.value.telemetry["control"]["reason"] == "state_lock_timeout"
    assert not list(ctl.directory.glob("*.lease"))


def test_release_wakes_a_waiter_without_waiting_for_periodic_recovery(tmp_path):
    ctl = controller(tmp_path)
    holder = acquire(ctl)
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(acquire, ctl, "waiting", "compile")
        wait_pending(ctl, 1)
        # Let this caller observe an unchanged occupied queue before the release.
        time.sleep(.03)
        started = time.monotonic()
        ctl.release(holder)
        lease = waiting.result(timeout=.15)
        assert time.monotonic() - started < .15
        ctl.release(lease)


def test_interactive_request_uses_reserved_capacity_amid_many_pending_callers(tmp_path):
    ctl = controller(tmp_path, limit=2, max_limit=2, reserve=1)
    holder = acquire(ctl, "running", "compile")
    def background(n):
        lease = acquire(ctl, str(n), "compile")
        ctl.release(lease)
    with ThreadPoolExecutor(max_workers=10) as pool:
        waiting = [pool.submit(background, n) for n in range(10)]
        wait_pending(ctl, 10)
        started = time.monotonic()
        interactive = acquire(ctl, "foreground", "interactive", timeout=.5)
        assert time.monotonic() - started < .5
        ctl.release(interactive)
        ctl.release(holder)
        for future in waiting:
            future.result(timeout=3)


def test_interrupted_wait_removes_its_ticket_and_keeps_the_running_owner(tmp_path, monkeypatch):
    ctl = controller(tmp_path)
    holder = acquire(ctl)
    def interrupt(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr("schedx.admission.time.sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        acquire(ctl, "cancelled", "compile")
    assert ctl.snapshot()["pending"] == 0
    assert ctl.snapshot()["active"] == 1
    ctl.release(holder)


def test_live_descendants_keep_the_slot_after_the_caller_releases_its_descriptor(tmp_path):
    ctl = controller(tmp_path)
    lease = acquire(ctl)
    group = tmp_path / "groups/schedx-agents/agent/tool"
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text("populated 1\n")
    ctl.bind_cgroup(lease, group)
    assert ctl.release(lease) is False
    with pytest.raises(AdmissionTimeout):
        acquire(ctl, timeout=0.05)
    (group / "cgroup.events").write_text("populated 0\n")
    snapshot = ctl.snapshot()
    assert snapshot["active"] == 0 and snapshot["reaped"] >= 1
    assert group.exists()  # Admission recovery never pretends to restore tool resources.


def test_separate_processes_share_one_concurrency_limit(tmp_path):
    code = """
import json,sys,time
from pathlib import Path
from schedx.admission import AdmissionConfig,ToolAdmission
c=ToolAdmission(Path(sys.argv[1]),AdmissionConfig(mode='fixed',initial_limit=1,max_limit=1,interactive_reserve=0))
s=time.monotonic();l=c.acquire(sys.argv[2],'interactive',submitted_at=s,deadline=s+5)
start=time.monotonic();time.sleep(.12);end=time.monotonic();c.release(l)
print(json.dumps({'start':start,'end':end,'wait':l.admitted_at-s}))
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "admission"), str(index)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for index in range(3)]
    try:
        rows = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=8)
            assert process.returncode == 0, stderr
            rows.append(json.loads(stdout))
        rows.sort(key=lambda row: row["start"])
        assert all(left["end"] <= right["start"] for left, right in zip(rows, rows[1:]))
        assert max(row["wait"] for row in rows) >= 0.1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_tool_inherits_the_lease_when_its_caller_dies(tmp_path):
    code = """
import os,subprocess,sys,time
from pathlib import Path
from schedx.admission import AdmissionConfig,ToolAdmission
c=ToolAdmission(Path(sys.argv[1]),AdmissionConfig(mode='fixed',initial_limit=1,max_limit=1,interactive_reserve=0),cgroup_root=Path(sys.argv[2]))
s=time.monotonic();l=c.acquire('crashing','interactive',submitted_at=s,deadline=s+5)
p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(.7)'],pass_fds=(l.fd,),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(p.pid,flush=True);os._exit(0)
"""
    crashed = subprocess.run([sys.executable, "-c", code, str(tmp_path / "admission"), str(tmp_path / "groups")],
                             capture_output=True, text=True, timeout=3, check=True)
    child_pid = int(crashed.stdout)
    ctl = controller(tmp_path)
    try:
        with pytest.raises(AdmissionTimeout):
            acquire(ctl, timeout=0.1)
        lease = acquire(ctl, timeout=3)
        assert lease.telemetry["reaped"] >= 1
        ctl.release(lease)
    finally:
        try:
            os.kill(child_pid, 15)
        except ProcessLookupError:
            pass


def test_pressure_feedback_is_bounded_and_missing_data_cannot_expand_capacity(tmp_path):
    totals = {"cpu": 0, "memory": 0, "io": 0}
    ctl = ToolAdmission(tmp_path, AdmissionConfig(initial_limit=4, min_limit=1, max_limit=5),
                        pressure_reader=lambda: dict(totals))
    state = ctl._empty()
    ctl._adapt(state, 0)
    for now in (1, 2):
        totals["cpu"] += 250_000
        ctl._adapt(state, now)
    assert state["limit"] == 2 and state["control"]["reason"] == "pressure_high"
    totals.pop("memory")
    ctl._adapt(state, 6)
    assert state["limit"] == 2 and state["control"]["reason"] == "pressure_unavailable"
    totals["memory"] = 0
    state["jobs"]["demand"] = {"status": "queued"}
    for now in range(7, 11):
        ctl._adapt(state, now)
    assert state["limit"] == 3 and state["control"]["reason"] == "healthy_demand"


def test_old_timeouts_do_not_permanently_disable_capacity_recovery(tmp_path):
    ctl = ToolAdmission(tmp_path, AdmissionConfig(initial_limit=1, max_limit=2, interactive_reserve=0),
                        pressure_reader=lambda: {"cpu": 0, "memory": 0, "io": 0})
    state = ctl._empty()
    state["jobs"]["waiting"] = {"status": "queued"}
    state["completions"] = [{"at": 0, "execution_timeout": True, "intent": "compile", "end_to_end_seconds": 1}]
    for now in range(31, 35):
        ctl._adapt(state, now)
    assert state["limit"] == 2


def test_memory_pressure_reduces_capacity_without_cpu_saturation(tmp_path):
    totals = {"cpu": 0, "memory": 0, "io": 0}
    ctl = ToolAdmission(tmp_path, AdmissionConfig(initial_limit=4), pressure_reader=lambda: dict(totals))
    state = ctl._empty()
    for now in range(3):
        totals["memory"] = now * 150_000
        ctl._adapt(state, now)
    assert state["limit"] == 2 and state["control"]["reason"] == "pressure_high"


class FilesystemRunner(ToolCallRunner):
    def _prepare_group(self, parent, tool, profile):
        tool.mkdir(parents=True)
        (tool / "cgroup.procs").write_text("")

    @staticmethod
    def _cleanup(tool, parent):
        (tool / "cgroup.procs").unlink(missing_ok=True)
        return ToolCallRunner._cleanup(tool, parent)


def test_waiting_timeout_never_launches_the_actual_tool(tmp_path):
    ctl = controller(tmp_path)
    holder = acquire(ctl)
    runner = FilesystemRunner(root=tmp_path / "groups", state_dir=tmp_path / "results", native_scx=False, admission=ctl)
    marker = tmp_path / "executed"
    try:
        result = runner.run([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"], timeout=0.08)
        assert result["returncode"] == 124 and result["timeout_phase"] == "queue"
        assert not result["command_started"] and not marker.exists()
        assert not (tmp_path / "groups").exists()
        assert result["execution_seconds"] == 0 and result["queue_wait_seconds"] >= 0.08
    finally:
        ctl.release(holder)


def test_queue_wait_consumes_the_execution_budget(tmp_path):
    ctl = controller(tmp_path)
    holder = acquire(ctl)
    timer = threading.Timer(0.2, ctl.release, args=(holder,))
    timer.start()
    runner = FilesystemRunner(root=tmp_path / "groups", state_dir=tmp_path / "results", native_scx=False, admission=ctl)
    try:
        result = runner.run([sys.executable, "-c", "import time; time.sleep(.4)"], timeout=0.5)
        assert result["returncode"] == 124 and result["timeout_phase"] == "execution"
        assert result["queue_wait_seconds"] >= 0.15
        assert result["command_started"] and result["duration_seconds"] < 0.8
        assert result["cleanup"]["admission_released"] and ctl.snapshot()["active"] == 0
    finally:
        timer.join()


def test_expired_setup_budget_keeps_the_actual_command_blocked(tmp_path):
    class SlowSetup(FilesystemRunner):
        def _prepare_group(self, *args):
            super()._prepare_group(*args)
            time.sleep(0.08)
    ctl = controller(tmp_path)
    marker = tmp_path / "executed"
    runner = SlowSetup(root=tmp_path / "groups", state_dir=tmp_path / "results", native_scx=False, admission=ctl)
    result = runner.run([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"], timeout=0.05)
    assert result["returncode"] == 124 and result["timeout_phase"] == "startup"
    assert not result["command_started"] and not marker.exists()
    assert ctl.snapshot()["active"] == 0


def test_cpu_contract_rejection_does_not_leak_admission_capacity(tmp_path, monkeypatch):
    from schedx.tool_runner import CpuQuotaUnavailable
    monkeypatch.setattr(ScxController, "state", lambda self: "enabled")
    ctl = controller(tmp_path)
    runner = FilesystemRunner(root=tmp_path / "groups", state_dir=tmp_path / "results", admission=ctl)
    with pytest.raises(CpuQuotaUnavailable):
        runner.run([sys.executable, "-c", "print('must not run')"], intent="background")
    assert ctl.snapshot()["active"] == ctl.snapshot()["pending"] == 0


def test_cli_exposes_shared_admission_and_rejects_invalid_configuration(capsys):
    parser = build_parser()
    args = parser.parse_args(["tool-run", "--admission", "adaptive", "--admission-state", "/tmp/shared-admission",
                              "--admission-limit", "4", "--admission-max", "2", "--", "true"])
    assert args.admission_state == "/tmp/shared-admission"
    assert cmd_tool_run(args) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["command_started"] is False
    assert result["status"] == "tool_request_rejected"
    assert parser.parse_args(["tool-run", "--", "true"]).admission == "off"
