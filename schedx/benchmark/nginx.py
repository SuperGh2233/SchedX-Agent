from __future__ import annotations

import csv
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from schedx.benchmark.wrk_parser import parse_wrk_output
from schedx.probes.cgroup_probe import CgroupProbe


@dataclass
class NginxBenchmarkConfig:
    url: str = "http://127.0.0.1/"
    duration: int = 30
    connections: int = 64
    threads: int = 4
    repeats: int = 3
    stress_cpu: int = 4
    output: Path = Path("results/nginx_benchmark")
    warmup: int = 3


class NginxStressBenchmark:
    phases = ("baseline", "interference", "schedx")

    def run(self, config: NginxBenchmarkConfig) -> dict[str, Any]:
        run_dir = config.output / time.strftime("%Y-%m-%d_%H-%M-%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        env = collect_environment()
        _write_json(run_dir / "env.json", env)
        _write_json(run_dir / "status.json", {"status": "started", "timestamp": int(time.time())})

        if not env["wrk_available"]:
            message = "wrk not found. Please install wrk or run scripts/install_wrk.sh"
            result = {"status": "failed", "error": message, "run_dir": str(run_dir)}
            _write_json(run_dir / "summary.json", result)
            _write_report(run_dir, config, env, [], result)
            return result
        if not env["stress_ng_available"]:
            message = "stress-ng not found. Please install stress-ng with dnf."
            result = {"status": "failed", "error": message, "run_dir": str(run_dir)}
            _write_json(run_dir / "summary.json", result)
            _write_report(run_dir, config, env, [], result)
            return result

        rows: list[dict[str, Any]] = []
        cleanup: dict[str, Any] = {}
        cgroup_evidence: dict[str, Any] = {}
        try:
            self._cleanup_stress()
            rows.extend(self._run_phase("baseline", config, run_dir))

            stress = self._start_stress(config)
            time.sleep(config.warmup)
            _write_json(run_dir / "interference_classify.json", _run_schedx_json(["classify", "--top", "50"]))
            rows.extend(self._run_phase("interference", config, run_dir))
            self._stop_stress(stress)
            _write_json(run_dir / "rollback_after_interference.json", _run_schedx_json(["rollback"]))

            stress = self._start_stress(config)
            time.sleep(config.warmup)
            _write_json(run_dir / "schedx_classify.json", _run_schedx_json(["classify", "--top", "50"]))
            _write_json(run_dir / "cgroup_snapshot_before.json", _snapshot_schedx_cgroup())
            optimize = _run_schedx_json(["optimize", "--target", "stress-ng", "--mode", "isolate_background"])
            _write_json(run_dir / "schedx_optimize.json", optimize)
            after_optimize = _snapshot_schedx_cgroup()
            _write_json(run_dir / "cgroup_snapshot_after_optimize.json", after_optimize)
            cgroup_evidence = build_cgroup_evidence(optimize, after_optimize)
            rows.extend(self._run_phase("schedx", config, run_dir))
            self._stop_stress(stress)
            rollback = _run_schedx_json(["rollback"])
            _write_json(run_dir / "rollback.json", rollback)
        finally:
            cleanup = self._final_cleanup()

        _write_summary_csv(run_dir / "summary.csv", rows)
        summary = build_summary(config, env, rows, cleanup, cgroup_evidence)
        _write_json(run_dir / "summary.json", summary)
        _write_report(run_dir, config, env, rows, summary)
        return {"status": "ok", "run_dir": str(run_dir), "summary": summary}

    def _run_phase(self, phase: str, config: NginxBenchmarkConfig, run_dir: Path) -> list[dict[str, Any]]:
        rows = []
        for repeat in range(1, config.repeats + 1):
            command = [
                "wrk",
                f"-t{config.threads}",
                f"-c{config.connections}",
                f"-d{config.duration}s",
                "--latency",
                config.url,
            ]
            completed = subprocess.run(command, check=False, capture_output=True, text=True)
            raw_path = run_dir / f"{phase}_wrk_repeat{repeat}.txt"
            raw_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
            metrics = parse_wrk_output(completed.stdout)
            note = "" if completed.returncode == 0 and metrics else "wrk failed or metrics missing"
            rows.append({"phase": phase, "repeat": repeat, **metrics, "notes": note})
        return rows

    def _start_stress(self, config: NginxBenchmarkConfig) -> subprocess.Popen:
        timeout = config.duration * max(config.repeats, 1) + config.warmup + 20
        return subprocess.Popen(
            ["stress-ng", "--cpu", str(config.stress_cpu), "--timeout", f"{timeout}s", "--metrics-brief"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _stop_stress(self, process: subprocess.Popen | None = None) -> None:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        self._cleanup_stress()

    def _cleanup_stress(self) -> None:
        subprocess.run(["pkill", "-f", "stress-ng"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _final_cleanup(self) -> dict[str, Any]:
        self._cleanup_stress()
        rollback = _run_schedx_json(["rollback"])
        base_exists = Path("/sys/fs/cgroup/schedx").exists()
        return {"rollback": rollback, "schedx_base_removed": not base_exists}


def collect_environment() -> dict[str, Any]:
    cgroup = CgroupProbe()
    return {
        "os_release": _read_text(Path("/etc/os-release")),
        "kernel": platform.platform(),
        "uname": _run_text(["uname", "-a"]),
        "cpu_count": os.cpu_count(),
        "memory": _run_text(["free", "-h"]),
        "python_version": sys.version,
        "cgroup_v2": cgroup.is_v2(),
        "cgroup_controllers": " ".join(cgroup.controllers()),
        "cgroup_subtree_control": _read_text(Path("/sys/fs/cgroup/cgroup.subtree_control")).strip(),
        "sched_ext_available": Path("/sys/kernel/sched_ext").exists(),
        "mode": "sched_ext-ready" if Path("/sys/kernel/sched_ext").exists() else "cgroup-only fallback",
        "nginx_status": _run_text(["systemctl", "is-active", "nginx"]).strip(),
        "wrk_available": shutil.which("wrk") is not None,
        "stress_ng_available": shutil.which("stress-ng") is not None,
    }


def build_summary(
    config: NginxBenchmarkConfig,
    env: dict[str, Any],
    rows: list[dict[str, Any]],
    cleanup: dict[str, Any],
    cgroup_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    phases = {phase: _phase_means(rows, phase) for phase in NginxStressBenchmark.phases}
    baseline = phases["baseline"]
    interference = phases["interference"]
    schedx = phases["schedx"]
    return {
        "config": {**asdict(config), "output": str(config.output)},
        "environment": {
            "os": env.get("os_release", ""),
            "kernel": env.get("uname", env.get("kernel", "")),
            "cpu_count": env.get("cpu_count"),
            "cgroup_v2": env.get("cgroup_v2"),
            "sched_ext_available": env.get("sched_ext_available"),
            "mode": env.get("mode"),
        },
        "phases": phases,
        "improvement": {
            "rps_drop_due_to_interference_percent": _pct_drop(
                baseline.get("mean_requests_per_sec"), interference.get("mean_requests_per_sec")
            ),
            "rps_recovery_vs_interference_percent": _pct_gain(
                interference.get("mean_requests_per_sec"), schedx.get("mean_requests_per_sec")
            ),
            "latency_increase_due_to_interference_percent": _pct_gain(
                baseline.get("mean_latency_avg_ms"), interference.get("mean_latency_avg_ms")
            ),
            "latency_reduction_vs_interference_percent": _pct_drop(
                interference.get("mean_latency_avg_ms"), schedx.get("mean_latency_avg_ms")
            ),
            "p99_reduction_vs_interference_percent": _pct_drop(
                interference.get("mean_p99_ms"), schedx.get("mean_p99_ms")
            ),
        },
        "cleanup": cleanup,
        "cgroup_evidence": cgroup_evidence or {},
    }


def build_cgroup_evidence(optimize: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    data = optimize.get("data") or {}
    action_by_pid: dict[str, dict[str, Any]] = {}
    for result in data.get("results") or []:
        pid = result.get("pid")
        if pid is None:
            continue
        action = result.get("action") or {}
        metadata = action.get("metadata") or {}
        action_by_pid[str(pid)] = {
            "status": result.get("status"),
            "group": result.get("group"),
            "comm": metadata.get("comm", ""),
            "matched_by": metadata.get("matched_by", ""),
            "target_weight": action.get("value"),
        }

    groups = []
    for group in snapshot.get("groups", []):
        procs = [str(pid) for pid in group.get("cgroup_procs", [])]
        pid = procs[0] if procs else ""
        action = action_by_pid.get(pid, {})
        groups.append(
            {
                "pid": pid,
                "path": group.get("path", ""),
                "cpu_weight": group.get("cpu_weight", ""),
                "cgroup_procs": procs,
                "action_status": action.get("status", ""),
                "comm": action.get("comm", ""),
                "matched_by": action.get("matched_by", ""),
                "target_weight": action.get("target_weight", ""),
            }
        )

    return {
        "optimize_returncode": optimize.get("returncode"),
        "matched_action_count": len(action_by_pid),
        "cgroup_count": len(groups),
        "groups": groups,
    }


def _phase_means(rows: list[dict[str, Any]], phase: str) -> dict[str, float | None]:
    phase_rows = [row for row in rows if row["phase"] == phase]
    return {
        "mean_requests_per_sec": _mean(row.get("requests_per_sec") for row in phase_rows),
        "mean_latency_avg_ms": _mean(row.get("latency_avg_ms") for row in phase_rows),
        "mean_p99_ms": _mean(row.get("p99_ms") for row in phase_rows),
    }


def _write_summary_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "phase",
        "repeat",
        "requests_per_sec",
        "latency_avg_ms",
        "latency_stdev_ms",
        "latency_max_ms",
        "p50_ms",
        "p75_ms",
        "p90_ms",
        "p99_ms",
        "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _write_report(
    run_dir: Path,
    config: NginxBenchmarkConfig,
    env: dict[str, Any],
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    phases = summary.get("phases", {}) if isinstance(summary, dict) else {}
    improvement = summary.get("improvement", {}) if isinstance(summary, dict) else {}
    cleanup = summary.get("cleanup", {}) if isinstance(summary, dict) else {}
    cgroup_evidence = summary.get("cgroup_evidence", {}) if isinstance(summary, dict) else {}
    lines = [
        "# SchedX-Agent Nginx Mixed Workload Benchmark Report",
        "",
        "## 1. Environment",
        "",
        f"- OS: {_one_line(env.get('os_release', ''))}",
        f"- Kernel: {env.get('uname', env.get('kernel', ''))}",
        f"- CPU cores: {env.get('cpu_count')}",
        f"- cgroup version: {'v2' if env.get('cgroup_v2') else 'unknown'}",
        f"- sched_ext: {env.get('sched_ext_available')}",
        f"- Agent mode: {env.get('mode')}",
        "",
        "## 2. Benchmark Configuration",
        "",
        f"- URL: {config.url}",
        f"- Duration: {config.duration}",
        f"- Connections: {config.connections}",
        f"- Threads: {config.threads}",
        f"- Repeats: {config.repeats}",
        f"- Stress CPU workers: {config.stress_cpu}",
        "",
        "## 3. Experiment Design",
        "",
        "This benchmark compares three phases:",
        "",
        "1. Baseline: nginx only.",
        "2. Interference: nginx + stress-ng CPU interference.",
        "3. SchedX: nginx + stress-ng + SchedX cgroup-based isolation.",
        "",
        "## 4. Results",
        "",
        "| Phase | Requests/sec | Avg Latency (ms) | P99 Latency (ms) |",
        "|---|---:|---:|---:|",
    ]
    for phase in NginxStressBenchmark.phases:
        data = phases.get(phase, {})
        lines.append(
            f"| {phase.title()} | {_fmt(data.get('mean_requests_per_sec'))} | "
            f"{_fmt(data.get('mean_latency_avg_ms'))} | {_fmt(data.get('mean_p99_ms'))} |"
        )
    lines.extend(
        [
            "",
            "## 5. Improvement Analysis",
            "",
            f"- RPS drop due to interference: {_fmt_pct(improvement.get('rps_drop_due_to_interference_percent'))}",
            f"- RPS recovery with SchedX: {_fmt_pct(improvement.get('rps_recovery_vs_interference_percent'))}",
            f"- Latency increase due to interference: {_fmt_pct(improvement.get('latency_increase_due_to_interference_percent'))}",
            f"- Latency reduction with SchedX: {_fmt_pct(improvement.get('latency_reduction_vs_interference_percent'))}",
            f"- P99 reduction with SchedX: {_fmt_pct(improvement.get('p99_reduction_vs_interference_percent'))}",
            "",
            "## 6. Cgroup Evidence",
            "",
            "SchedX applies cgroup v2 control to stress-ng processes when matching processes are present.",
            "",
            "- cgroup path: `/sys/fs/cgroup/schedx/pid-<pid>/`",
            "- target cpu.weight: 50",
            "- rollback restores previous cpu.weight and cleans empty cgroups",
            f"- optimize return code: {cgroup_evidence.get('optimize_returncode', 'n/a')}",
            f"- matched isolate actions: {cgroup_evidence.get('matched_action_count', 0)}",
            "",
            "| PID | Command | Matched By | cgroup Path | cpu.weight | cgroup.procs | Action Status |",
            "|---:|---|---|---|---:|---|---|",
        ]
    )
    groups = cgroup_evidence.get("groups") or []
    if groups:
        for group in groups:
            lines.append(
                f"| {group.get('pid', '')} | {group.get('comm', '')} | {group.get('matched_by', '')} | "
                f"`{group.get('path', '')}` | {group.get('cpu_weight', '')} | "
                f"{','.join(group.get('cgroup_procs', []))} | {group.get('action_status', '')} |"
            )
    else:
        lines.append("| n/a | n/a | n/a | n/a | n/a | n/a | no cgroup evidence captured |")
    lines.extend(
        [
            "",
            f"- cleanup status: {json.dumps(cleanup, ensure_ascii=False)}",
            "",
            "## 7. Conclusion",
            "",
            "In the mixed workload scenario with nginx and stress-ng, stress-ng introduces CPU interference and may affect nginx performance. SchedX-Agent identifies stress-ng as background_noise and applies cgroup v2 CPU weight control to isolate the interfering workload. The benchmark results above honestly show whether nginx throughput and latency recover under SchedX control.",
        ]
    )
    if summary.get("status") == "failed":
        lines.extend(["", f"Benchmark failed: {summary.get('error')}"])
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _snapshot_schedx_cgroup() -> dict[str, Any]:
    base = Path("/sys/fs/cgroup/schedx")
    if not base.exists():
        return {"exists": False, "groups": []}
    groups = []
    for child in sorted(base.glob("pid-*")):
        groups.append(
            {
                "path": str(child),
                "cpu_weight": _read_text(child / "cpu.weight").strip(),
                "cgroup_procs": _read_text(child / "cgroup.procs").split(),
            }
        )
    return {"exists": True, "groups": groups, "entries": sorted(path.name for path in base.iterdir())}


def _run_schedx_json(args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "schedx.main", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        data = {}
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "data": data,
    }


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _run_text(command: list[str]) -> str:
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout or completed.stderr).strip()


def _mean(values) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 4)


def _pct_drop(before, after) -> float | None:
    if before in (None, 0) or after is None:
        return None
    return round(((float(before) - float(after)) / float(before)) * 100.0, 4)


def _pct_gain(before, after) -> float | None:
    if before in (None, 0) or after is None:
        return None
    return round(((float(after) - float(before)) / float(before)) * 100.0, 4)


def _fmt(value) -> str:
    if value is None:
        return ""
    return f"{float(value):.3f}"


def _fmt_pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.2f}%"


def _one_line(value: Any) -> str:
    return str(value).splitlines()[0] if value else ""
