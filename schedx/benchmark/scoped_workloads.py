"""Disposable Linux services shared by version comparisons and Agent scenarios.

Only processes launched here and their private cgroups are stopped. No global
process-name cleanup or installed service restart is used.
"""

from __future__ import annotations

import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def process_tree(leader: int, proc_root: Path = Path("/proc")) -> list[int]:
    pending, found = [leader], set()
    while pending:
        pid = pending.pop()
        if pid in found:
            continue
        try:
            if os.getpgid(pid) != leader:
                continue
            children = (proc_root / str(pid) / "task" / str(pid) / "children").read_text()
        except (ProcessLookupError, FileNotFoundError):
            continue
        found.add(pid)
        pending.extend(int(value) for value in children.split())
    return sorted(found)


def process_ticks(pids: list[int]) -> int:
    ticks = 0
    for pid in set(pids):
        try:
            text = Path(f"/proc/{pid}/stat").read_text()
            fields = text[text.rfind(")") + 2:].split()
            ticks += int(fields[11]) + int(fields[12])
        except (FileNotFoundError, ProcessLookupError):
            continue
    return ticks


class OwnedWorkloads:
    def __init__(self, output: Path, *, workers: int = 3, redis: bool = False,
                 cpus: set[int] | None = None) -> None:
        self.output = output.resolve()
        self.workers = workers
        self.with_redis = redis
        self.cpus = cpus
        self.identifier = "schedx-experiment-" + uuid.uuid4().hex
        self.cgroup_root = Path("/sys/fs/cgroup") / self.identifier
        self.groups = {name: self.cgroup_root / name for name in ("service", "redis", "noise", "batch")}
        self.processes: dict[str, subprocess.Popen] = {}
        self.logs = []
        self.nginx_port = available_port()
        self.redis_port = available_port()
        self.error_flag = self.output / "respond-error"
        self.cleanup = {"status": "not_run"}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.nginx_port}/"

    def __enter__(self) -> OwnedWorkloads:
        required = ["nginx", "wrk", "stress-ng"] + (["redis-server", "redis-cli", "redis-benchmark"] if self.with_redis else [])
        missing = [name for name in required if shutil.which(name) is None]
        if missing:
            raise RuntimeError(f"missing workload tools: {missing}")
        if not sys.platform.startswith("linux") or os.geteuid() != 0:
            raise RuntimeError("owned cgroup experiments require Linux root")
        self.output.mkdir(parents=True, exist_ok=False)
        try:
            self.cgroup_root.mkdir(exist_ok=False)
            for group in self.groups.values():
                group.mkdir()
            # All file names here are under a new owned directory. Quote the
            # absolute flag path for nginx's configuration syntax.
            flag = str(self.error_flag).replace("\\", "\\\\").replace('"', '\\"')
            config = (
                "user root; daemon off; master_process on; worker_processes 1;\n"
                "pid nginx.pid; error_log nginx-error.log;\n"
                "events { worker_connections 1024; }\n"
                "http { access_log off; server { listen 127.0.0.1:"
                f"{self.nginx_port}; location / {{ if (-f \"{flag}\") {{ return 503; }} "
                "return 200 'schedx-owned-service'; } } }\n"
            )
            config_path = self.output / "nginx.conf"
            config_path.write_text(config)
            self.spawn("service", [shutil.which("nginx"), "-p", str(self.output) + "/", "-c", str(config_path)])
            self._wait_http()
            if self.with_redis:
                self.spawn("redis", [shutil.which("redis-server"), "--bind", "127.0.0.1", "--port", str(self.redis_port),
                                     "--save", "", "--appendonly", "no", "--dir", str(self.output)])
                self._wait_redis()
            return self
        except BaseException:
            self.close()
            raise

    def spawn(self, role: str, command: list[str]) -> subprocess.Popen:
        if role in self.processes and self.processes[role].poll() is None:
            raise RuntimeError(f"owned workload already active: {role}")
        ready_r, ready_w = os.pipe()
        gate_r, gate_w = os.pipe()
        descriptors = [ready_r, ready_w, gate_r, gate_w]
        log = (self.output / f"{role}-{len(self.logs)}.log").open("w")
        self.logs.append(log)
        try:
            launcher = Path(__file__).resolve().parents[1] / "tool_child.py"
            process = subprocess.Popen(
                [sys.executable, str(launcher), str(ready_w), str(gate_r), str(self.groups[role]), *command],
                stdout=log, stderr=log, pass_fds=(ready_w, gate_r), start_new_session=True,
            )
            self.processes[role] = process
            for descriptor in (ready_w, gate_r):
                os.close(descriptor)
                descriptors.remove(descriptor)
            if not select.select([ready_r], [], [], 5)[0] or os.read(ready_r, 1) != b"R":
                raise RuntimeError(f"{role} did not enter its owned cgroup")
            if self.cpus:
                os.sched_setaffinity(process.pid, self.cpus)
            os.write(gate_w, b"G")
            return process
        finally:
            for descriptor in descriptors:
                os.close(descriptor)

    def _wait_http(self) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.processes["service"].poll() is not None:
                raise RuntimeError("private nginx exited during startup; inspect its log")
            try:
                with urllib.request.urlopen(self.url, timeout=0.5) as response:
                    if response.read() == b"schedx-owned-service":
                        return
            except OSError:
                pass
            time.sleep(0.05)
        raise RuntimeError("private nginx startup deadline expired")

    def _wait_redis(self) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.processes["redis"].poll() is not None:
                raise RuntimeError("private Redis exited during startup")
            result = subprocess.run(["redis-cli", "-h", "127.0.0.1", "-p", str(self.redis_port), "PING"],
                                    capture_output=True, text=True, timeout=1)
            if result.returncode == 0 and result.stdout.strip() == "PONG":
                return
            time.sleep(0.05)
        raise RuntimeError("private Redis startup deadline expired")

    def noise(self, enabled: bool) -> None:
        if enabled:
            if "noise" not in self.processes or self.processes["noise"].poll() is not None:
                self.spawn("noise", ["stress-ng", "--cpu", str(self.workers), "--timeout", "600s", "--metrics-brief"])
                time.sleep(0.2)
        else:
            self.stop("noise")

    def restart_service(self) -> None:
        """Start a new owned service identity for a separate fault trial."""
        self.stop("service")
        config = self.output / "nginx.conf"
        self.spawn("service", [shutil.which("nginx"), "-p", str(self.output) + "/", "-c", str(config)])
        self._wait_http()

    def pids(self, role: str | None = None) -> list[int]:
        return sorted({pid for name, process in self.processes.items()
                       if (role is None or name == role) and process.poll() is None
                       for pid in process_tree(process.pid)})

    def stop(self, role: str) -> None:
        process = self.processes.get(role)
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        # A launcher may exit before its descendants. The cgroup remains a
        # precise ownership boundary even after the process leader has exited.
        group = self.groups[role]
        if group.exists() and (group / "cgroup.procs").read_text().strip():
            kill_file = group / "cgroup.kill"
            if kill_file.exists():
                kill_file.write_text("1")
            else:
                for pid in (group / "cgroup.procs").read_text().split():
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            deadline = time.monotonic() + 5
            while (group / "cgroup.procs").read_text().strip() and time.monotonic() < deadline:
                time.sleep(0.05)

    def close(self) -> None:
        errors = []
        for role in self.groups:
            try:
                self.stop(role)
                if self.groups[role].exists():
                    self.groups[role].rmdir()
            except Exception as exc:
                errors.append(f"{role}: {exc}")
        try:
            if self.cgroup_root.exists():
                self.cgroup_root.rmdir()
        except OSError as exc:
            errors.append(str(exc))
        for log in self.logs:
            log.close()
        self.cleanup = {"status": "passed" if not errors else "failed", "errors": errors,
                        "cgroup_removed": not self.cgroup_root.exists(),
                        "leaders_reaped": all(process.poll() is not None for process in self.processes.values())}

    def __exit__(self, *args) -> None:
        self.close()
