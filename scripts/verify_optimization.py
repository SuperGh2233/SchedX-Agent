#!/usr/bin/env python3
"""Real-kernel recovery, tool lifecycle, network and fairness verification."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schedx.controllers.cgroup_controller import CgroupController
from schedx.controllers.ebpf_controller import EbpfController, EbpfProgType
from schedx.controllers.process_state import ProcessState
from schedx.scx_daemon import ScxDaemonClient
from schedx.state import atomic_json
from schedx.tool_runner import ToolCallRunner


def recovery(output: Path) -> dict:
    child = subprocess.Popen(["sleep", "60"])
    ctl = CgroupController(managed_prefix="schedx-verify-" + uuid.uuid4().hex[:8], rollback_file=output / "recovery.json", dry_run=False)
    original_group = ctl._current_cgroup_path(child.pid)
    try:
        ctl.add_pid("worker", child.pid)
        ctl.set_cpu_weight("worker", 50)
        target = ctl.group_path("worker") / "cpu.weight"
        original_write = Path.write_text

        def fail(path, *args, **kwargs):
            if path == target:
                raise PermissionError("injected one-time restoration failure")
            return original_write(path, *args, **kwargs)

        with patch.object(Path, "write_text", fail):
            first = ctl.rollback()
        assert any(isinstance(row, dict) and row.get("status") == "failed" for row in first)
        assert ctl.rollback_file.exists()
        CgroupController(managed_prefix=ctl.managed_prefix, rollback_file=ctl.rollback_file, dry_run=False).rollback()
        assert not ctl.rollback_file.exists()
        assert ctl._current_cgroup_path(child.pid) == original_group
        process = ProcessState(output / "process.json", "verification")
        previous_nice = os.getpriority(os.PRIO_PROCESS, child.pid)
        process.record(child.pid, "nice", previous_nice)
        os.setpriority(os.PRIO_PROCESS, child.pid, 5)
        assert process.rollback()[0]["status"] == "restored"
        assert os.getpriority(os.PRIO_PROCESS, child.pid) == previous_nice
        return {"status": "passed", "journal_retry": True, "original_cgroup_restored": True, "nice_restored": True}
    finally:
        ctl.rollback()
        child.terminate()
        child.wait()


def tools(output: Path) -> dict:
    runner = ToolCallRunner(state_dir=output / "tools")
    check = "import json,os; from pathlib import Path; from schedx.scx_daemon import ScxDaemonClient; g=Path('/sys/fs/cgroup')/Path('/proc/self/cgroup').read_text().split('0::')[1].strip().lstrip('/'); p=ScxDaemonClient().request('policies')['cgroup_policies']; print(json.dumps({'registered_before_exec':str(g.stat().st_ino) in p}))"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: runner.run([sys.executable, "-c", check], agent_id="verification", intent="test"), range(4)))
    assert all(row["returncode"] == 0 and row["native_scx"] for row in results)
    assert all(json.loads(row["stdout"])["registered_before_exec"] for row in results)
    assert all(row["cleanup"]["cgroup_removed"] and row["cleanup"]["policy_removed"] for row in results)
    timeout = runner.run([sys.executable, "-c", "import subprocess,time; subprocess.Popen(['sleep','60']); time.sleep(60)"], timeout=1)
    assert timeout["timed_out"] and timeout["cleanup"]["cgroup_removed"]
    large = runner.run([sys.executable, "-c", "print('x'*200000)"], output_limit=8192)
    assert large["stdout_truncated"] and Path(large["stdout_path"]).stat().st_size == 8192
    return {"status": "passed", "concurrent_tools": 4, "registration_before_exec": True, "timeout_tree_cleanup": timeout["cleanup"], "bounded_output": True}


def cpu_controls(output: Path) -> dict:
    """Observe actual controller semantics under the currently loaded scheduler."""
    runner = ToolCallRunner(state_dir=output / "cpu-controls", native_scx=False)
    code = "import time; start=time.process_time(); end=time.monotonic()+2\nwhile time.monotonic()<end: pass\nprint(time.process_time()-start)"
    # This probe deliberately observes whether the active backend enforces the
    # configured quota; it does not request a guaranteed hard quota.
    quota = runner.run([sys.executable, "-c", code], profile_overrides={"cpu_max": "10000 100000"}, cpu_limit_mode="soft")
    assert quota["returncode"] == 0
    cpu = min(os.sched_getaffinity(0))
    weight_code = "import time,os,sys; os.sched_setaffinity(0,{int(sys.argv[1])}); start=time.process_time(); end=time.monotonic()+3\nwhile time.monotonic()<end: pass\nprint(time.process_time()-start)"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(runner.run, [sys.executable, "-c", weight_code, str(cpu)], profile_overrides={"cpu_weight": weight}) for weight in (100, 1000)]
        rows = [future.result() for future in futures]
    assert all(row["returncode"] == 0 for row in rows)
    seconds = float(quota["stdout"].strip())
    low, high = [float(row["stdout"].strip()) for row in rows]
    return {"quota": "10000 100000", "quota_wall_seconds": 2, "quota_cpu_seconds": seconds, "quota_observed": seconds < 0.5,
            "weights": [100, 1000], "cpu_seconds": [low, high], "weight_runtime_ratio": high / low,
            "kernel": os.uname().release, "sched_ext_state": Path("/sys/kernel/sched_ext/state").read_text().strip()}


def network(output: Path) -> dict:
    name = "schedx-net-verify-" + uuid.uuid4().hex[:8]
    groups = CgroupController(managed_prefix=name, rollback_file=output / "network-recovery.json", dry_run=False)
    group = groups.create_group("sender")
    ctl = EbpfController(ebpf_dir=Path(__file__).resolve().parents[1] / "ebpf", pin_root=Path("/sys/fs/bpf") / name, dry_run=False)
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(0.1)
    received = [0]
    stop = threading.Event()

    def receive():
        while not stop.is_set():
            try:
                received[0] += len(server.recv(65536))
            except socket.timeout:
                pass

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    port = server.getsockname()[1]
    code = """import socket,sys,time,os
from pathlib import Path
Path(sys.argv[1]).joinpath('cgroup.procs').write_text(str(os.getpid()))
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
print('ready',flush=True)
for line in sys.stdin:
 end=time.monotonic()+float(line)
 while time.monotonic()<end:
  try: s.sendto(b'x'*1000,('127.0.0.1',int(sys.argv[2])))
  except PermissionError: pass
  time.sleep(0.002)
 print('done',flush=True)
"""
    sender = None
    try:
        assert ctl.load_program(EbpfProgType.NET_POLICY), ctl.last_error
        assert ctl.attach_program(EbpfProgType.NET_POLICY), ctl.last_error
        sender = subprocess.Popen([sys.executable, "-c", code, str(group), str(port)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert sender.stdout.readline().strip() == "ready"
        windows = []
        for rate in (30000, 180000, 30000):
            assert ctl.update_net_cgroup_policy(group.stat().st_ino, ctl.CLASS_BACKGROUND, rate_limit=rate, burst_size=16384)
            before = received[0]
            sender.stdin.write("3\n")
            sender.stdin.flush()
            assert sender.stdout.readline().strip() == "done"
            time.sleep(0.1)
            windows.append({"rate_bytes_per_second": rate, "delivered_bytes": received[0] - before})
        assert windows[1]["delivered_bytes"] > windows[2]["delivered_bytes"] * 2.5, windows
        before = ctl.get_stats().net_policy
        assert ctl.update_net_cgroup_policy(group.stat().st_ino, ctl.CLASS_LATENCY, rate_limit=1000, burst_size=1000)
        sender.stdin.write("1\n")
        sender.stdin.flush()
        assert sender.stdout.readline().strip() == "done"
        after = ctl.get_stats().net_policy
        assert after["dropped_packets"] == before["dropped_packets"]
        assert after["overlimit_allowed_packets"] > before["overlimit_allowed_packets"]
        return {"status": "passed", "same_cgroup_rate_switches": windows, "latency_overlimit_is_allowed": True, "stats": after}
    finally:
        if sender:
            sender.terminate()
            sender.wait()
            sender.stdin.close()
            sender.stdout.close()
        ctl.unload_program(EbpfProgType.NET_POLICY)
        groups.cleanup_empty_groups()
        stop.set()
        thread.join(timeout=2)
        server.close()


def fairness(
    output: Path, duration: int, *, vary_load: bool = False,
    load_phase_seconds: int = 60, latency_wait_ms: float = 2000,
    ordinary_wait_ms: float = 3000,
) -> dict:
    client = ScxDaemonClient(timeout=5)
    runner = ToolCallRunner(state_dir=output / "fairness-tools")
    cpu = min(os.sched_getaffinity(0))
    vary_load = vary_load or duration >= 7200
    busy = """import os, time, sys
os.sched_setaffinity(0, {int(sys.argv[1])})
start = time.monotonic()
end = start + float(sys.argv[2])
index = int(sys.argv[3])
vary = bool(int(sys.argv[4]))
phase = int(sys.argv[5])
n = 0
while time.monotonic() < end:
    n += 1
    if vary and int((time.monotonic() - start) // phase) % 3 == index % 3:
        time.sleep(0.001)
print(n)
"""
    intents = ["interactive"] * 4 + ["compile", "background"]
    wait_budgets = {"0": ordinary_wait_ms, "1": latency_wait_ms, "2": 2000, "3": 2000}
    samples = []
    with ThreadPoolExecutor(max_workers=len(intents)) as pool:
        futures = [pool.submit(runner.run, [sys.executable, "-c", busy, str(cpu), str(duration), str(index), str(int(vary_load)), str(load_phase_seconds)], agent_id="fairness-verification", intent=intent, timeout=duration+30, cpu_limit_mode="soft") for index, intent in enumerate(intents)]
        start = time.monotonic()
        previous = None
        failures = []
        recovery_checks = []
        next_recovery = 120
        while not all(future.done() for future in futures):
            time.sleep(min(5, max(1, duration // 3)))
            status = client.request("status")
            metrics = client.request("class_metrics")["class_metrics"]
            runtime = {int(key): row["runtime_ns"] for key, row in metrics.items()}
            delta = {key: value - previous.get(key, value) for key, value in runtime.items()} if previous else {}
            elapsed = time.monotonic() - start
            sample = {"elapsed": round(elapsed, 2), "scheduler_running": status["scheduler_running"], "fairness": status["fairness"], "runtime_delta_ns": delta, "class_metrics": metrics}
            samples.append(sample)
            atomic_json(output / "fairness-progress.json", {"duration": duration, "samples": samples, "failures": failures})
            if not status["scheduler_running"]:
                failures.append("scheduler exited unexpectedly")
            for key, budget in wait_budgets.items():
                message = f"class {key} exceeded {budget:g} ms queue wait budget"
                if metrics.get(key, {}).get("max_wait_ns", 0) / 1e6 > budget and message not in failures:
                    failures.append(message)
            if elapsed > 10 and elapsed < duration - 5:
                for key in (1, 2, 3):
                    if delta.get(key, 0) <= 0:
                        failures.append(f"class {key} made no runtime progress")
            previous = runtime
            if duration >= 7200 and elapsed >= next_recovery and elapsed < duration - 10:
                checked = runner.run([sys.executable, "-c", "import time; time.sleep(10)"], agent_id="recovery-monitor", timeout=0.3)
                recovery_checks.append(checked["cleanup"])
                if not checked["timed_out"] or not checked["cleanup"]["cgroup_removed"] or not checked["cleanup"]["policy_removed"]:
                    failures.append("periodic timeout recovery failed")
                next_recovery += 120
            if int(elapsed) % 30 < 5:
                print(json.dumps({"elapsed": round(elapsed), "duration": duration, "runtime_progress": delta, "failures": len(failures)}), flush=True)
        results = [future.result() for future in futures]
    assert all(row["returncode"] == 0 and row["cleanup"]["cgroup_removed"] and row["native_scx"] for row in results)
    max_wait_ms = {key: row["max_wait_ns"] / 1e6 for key, row in samples[-1]["class_metrics"].items()}
    return {"status": "failed" if failures else "passed", "failures": failures, "duration": duration, "single_cpu_contenders": len(intents), "samples": len(samples), "max_wait_ms": max_wait_ms, "wait_budgets_ms": wait_budgets, "load_pattern": f"rotating busy/sleep every {load_phase_seconds} seconds" if vary_load else "fixed saturation", "periodic_recoveries": len(recovery_checks), "tool_results": [{"intent": row["intent"], "duration": row["duration_seconds"], "cleanup": row["cleanup"]} for row in results]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--output", type=Path, default=Path("results/optimization-validation"))
    parser.add_argument("--fairness-only", action="store_true")
    parser.add_argument("--vary-load", action="store_true")
    parser.add_argument("--load-phase-seconds", type=int, default=60)
    parser.add_argument("--latency-wait-ms", type=float, default=2000)
    parser.add_argument("--ordinary-wait-ms", type=float, default=3000)
    args = parser.parse_args()
    if min(args.duration, args.load_phase_seconds, args.latency_wait_ms, args.ordinary_wait_ms) <= 0:
        parser.error("duration, load phase and queue wait budgets must be positive")
    if os.geteuid() != 0 or args.duration < 15:
        parser.error("root and duration >= 15 seconds are required")
    args.output.mkdir(parents=True, exist_ok=True)
    client = ScxDaemonClient(timeout=5)
    daemon = None
    if Path("/sys/kernel/sched_ext/state").read_text().strip() != "disabled":
        raise RuntimeError("verification requires the default scheduler initially; preserve existing scheduler state first")
    if client.is_available():
        raise RuntimeError("run verification with no pre-existing daemon so its state remains isolated")
    report = {"kernel": os.uname().release, "cpus": os.cpu_count(), "duration": args.duration, "cases": {}}
    try:
        if not args.fairness_only:
            report["cases"]["cpu_controls_default"] = cpu_controls(args.output)
        log = (args.output / "daemon.log").open("w")
        daemon = subprocess.Popen([sys.executable, "-m", "schedx", "scx-daemon", "run"], stdout=log, stderr=log)
        for _ in range(100):
            if client.is_available():
                break
            if daemon.poll() is not None:
                raise RuntimeError("verification daemon failed: " + (args.output / "daemon.log").read_text())
            time.sleep(0.1)
        assert client.is_available()
        if not args.fairness_only:
            report["cases"]["cpu_controls_native"] = cpu_controls(args.output)
            for name, case in (("recovery", recovery), ("tools", tools), ("network", network)):
                report["cases"][name] = case(args.output)
                atomic_json(args.output / "summary.json", report)
                print(json.dumps({"case": name, "status": "passed"}), flush=True)
        report["cases"]["fairness"] = fairness(
            args.output, args.duration, vary_load=args.vary_load,
            load_phase_seconds=args.load_phase_seconds,
            latency_wait_ms=args.latency_wait_ms, ordinary_wait_ms=args.ordinary_wait_ms,
        )
        assert report["cases"]["fairness"]["status"] == "passed", report["cases"]["fairness"]["failures"]
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        if daemon:
            try:
                client.request("shutdown")
            except (OSError, RuntimeError):
                daemon.terminate()
            try:
                daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()
            log.close()
        report["final_sched_ext_state"] = Path("/sys/kernel/sched_ext/state").read_text().strip()
        atomic_json(args.output / "summary.json", report)


if __name__ == "__main__":
    main()
