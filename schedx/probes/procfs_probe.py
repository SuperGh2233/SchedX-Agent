from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from schedx.probes.pressure_probe import PressureProbe
from schedx.probes.cgroup_probe import CgroupProbe


@dataclass
class ProcessSample:
    pid: int
    comm: str
    state: str
    ppid: int
    cmdline: str
    utime: int
    stime: int
    rss_bytes: int
    voluntary_ctxt_switches: int = 0
    nonvoluntary_ctxt_switches: int = 0
    cpu_percent: float = 0.0
    cgroup: str = ""
    sched: dict[str, float] | None = None


class ProcfsProbe:
    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root
        self.page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
        self.clock_ticks = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

    def _read_total_cpu_ticks(self) -> int:
        stat = self.proc_root / "stat"
        if not stat.exists():
            return 1
        first = stat.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        return sum(int(x) for x in first.split()[1:])

    def _read_process(self, pid: int) -> ProcessSample | None:
        stat_path = self.proc_root / str(pid) / "stat"
        status_path = self.proc_root / str(pid) / "status"
        sched_path = self.proc_root / str(pid) / "sched"
        cgroup_path = self.proc_root / str(pid) / "cgroup"
        cmdline_path = self.proc_root / str(pid) / "cmdline"
        try:
            raw = stat_path.read_text(encoding="utf-8", errors="replace").strip()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return None

        close = raw.rfind(")")
        open_ = raw.find("(")
        if open_ < 0 or close < 0:
            return None
        comm = raw[open_ + 1 : close]
        fields = raw[close + 2 :].split()
        if len(fields) < 22:
            return None

        state = fields[0]
        ppid = int(fields[1])
        utime = int(fields[11])
        stime = int(fields[12])
        rss_pages = int(fields[21])
        sample = ProcessSample(
            pid=pid,
            comm=comm,
            state=state,
            ppid=ppid,
            cmdline=self._read_cmdline(cmdline_path),
            utime=utime,
            stime=stime,
            rss_bytes=max(rss_pages, 0) * self.page_size,
        )

        try:
            for line in status_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("voluntary_ctxt_switches:"):
                    sample.voluntary_ctxt_switches = int(line.split()[1])
                elif line.startswith("nonvoluntary_ctxt_switches:"):
                    sample.nonvoluntary_ctxt_switches = int(line.split()[1])
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
        sample.sched = self._read_sched(sched_path)
        sample.cgroup = self._read_cgroup(cgroup_path)
        return sample

    def _read_cmdline(self, path: Path) -> str:
        try:
            raw = path.read_bytes()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return ""
        return " ".join(part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part)

    def _read_sched(self, path: Path) -> dict[str, float]:
        wanted = {
            "nr_switches",
            "se.sum_exec_runtime",
            "se.statistics.wait_sum",
            "se.statistics.wait_count",
            "se.statistics.iowait_sum",
        }
        result: dict[str, float] = {}
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return result
        for line in lines:
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            key = key.strip()
            if key not in wanted:
                continue
            value = raw.strip().split()[0] if raw.strip() else ""
            try:
                result[key] = float(value)
            except ValueError:
                continue
        return result

    def _read_cgroup(self, path: Path) -> str:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return ""
        for line in lines:
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1] == "":
                return parts[2]
        return lines[0] if lines else ""

    def list_processes(self, pids: list[int] | None = None) -> list[ProcessSample]:
        samples: list[ProcessSample] = []
        if pids is not None:
            if any(isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 for pid in pids):
                raise ValueError("scope PIDs must be positive integers")
            return [sample for pid in sorted(set(pids)) if (sample := self._read_process(pid)) is not None]
        if not self.proc_root.exists():
            return samples
        for entry in self.proc_root.iterdir():
            if entry.name.isdigit():
                sample = self._read_process(int(entry.name))
                if sample:
                    samples.append(sample)
        return samples

    def is_kernel_thread(self, proc: ProcessSample) -> bool:
        prefixes = ("kworker", "ksoftirqd", "migration", "rcu", "kthreadd", "cpuhp")
        if proc.cmdline:
            return False
        if proc.rss_bytes != 0:
            return False
        if proc.ppid not in (0, 2):
            return False
        return proc.comm.startswith(prefixes)

    def _workload_score(self, proc: ProcessSample) -> int:
        text = f"{proc.comm} {proc.cmdline}".lower()
        latency = ("nginx", "redis")
        noise = ("stress-ng", "stress")
        batch = ("sysbench", "gcc", "cc1", "make", "cmake")
        if any(name in text for name in latency + noise + batch):
            return 2
        if proc.rss_bytes > 0:
            return 1
        return 0

    def snapshot(self, interval: float = 0.2, top: int = 20, pids: list[int] | None = None) -> dict:
        before_total = self._read_total_cpu_ticks()
        before = {p.pid: p for p in self.list_processes(pids)}
        time.sleep(max(interval, 0.01))
        after_total = self._read_total_cpu_ticks()
        after = self.list_processes(pids)
        total_delta = max(after_total - before_total, 1)

        enriched: list[ProcessSample] = []
        for proc in after:
            prev = before.get(proc.pid)
            if prev:
                delta = (proc.utime + proc.stime) - (prev.utime + prev.stime)
                proc.cpu_percent = round((delta / total_delta) * 100.0 * os.cpu_count(), 2)
            if not self.is_kernel_thread(proc):
                enriched.append(proc)

        enriched.sort(key=lambda p: (self._workload_score(p), p.cpu_percent, p.rss_bytes), reverse=True)
        pressure = PressureProbe(self.proc_root).snapshot()
        return {
            "cpu_count": os.cpu_count(),
            "pressure": pressure,
            "cgroup": CgroupProbe().snapshot(),
            "processes": [asdict(p) for p in enriched[:top]],
            "process_scope": "host" if pids is None else "explicit_pids",
            "scope_pids": sorted(set(pids)) if pids is not None else None,
        }
