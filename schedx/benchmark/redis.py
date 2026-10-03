from __future__ import annotations

import csv
import io
import shutil
import subprocess
import time
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from schedx.agent.context import AgentContext
from schedx.agent.loop import AgentLoop
from schedx.benchmark.common import (
    cleanup_stress,
    cpu_ticks,
    describe,
    mean,
    percent_drop,
    percent_gain,
    process_ids,
    start_stress,
    stop_process,
    write_json,
)
from schedx.benchmark.nginx import collect_environment
from schedx.benchmark.redis_parser import parse_redis_benchmark_output
from schedx.controllers.scx_controller import ScxController
from schedx.skills.rollback_skill import RollbackSkill


@dataclass
class RedisBenchmarkConfig:
    host: str = "127.0.0.1"
    port: int = 6379
    duration: int = 10
    connections: int = 64
    threads: int = 4
    repeats: int = 3
    stress_cpu: int = 4
    warmup: int = 2
    minimum_background_retention_percent: float = 25.0
    output: Path = Path("results/redis-benchmark")

    @property
    def requests(self) -> int:
        # Fixed work is more comparable than killing a looping client mid-command.
        return self.duration * 50_000


class RedisMixedBenchmark:
    phases = ("baseline", "interference", "schedx")

    def run(self, config: RedisBenchmarkConfig) -> dict[str, Any]:
        self._validate(config)
        run_dir = config.output / time.strftime("%Y-%m-%d_%H-%M-%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        environment = collect_environment()
        environment["redis_status"] = self._redis_ping(config)
        write_json(run_dir / "env.json", environment)

        missing = [
            tool for tool in ("redis-benchmark", "stress-ng") if not shutil.which(tool)
        ]
        if missing:
            return self._fail(run_dir, f"missing required tools: {', '.join(missing)}")
        if environment["redis_status"] != "PONG":
            return self._fail(
                run_dir,
                f"Redis is not reachable at {config.host}:{config.port}",
            )
        if ScxController().state() == "enabled":
            return self._fail(
                run_dir,
                "sched_ext is already enabled; stop it before a clean Redis comparison",
            )

        rows: list[dict[str, Any]] = []
        evidence: dict[str, list[dict[str, Any]]] = {
            phase: [] for phase in self.phases
        }
        execution_order: list[dict[str, Any]] = []
        try:
            for repeat in range(1, config.repeats + 1):
                offset = (repeat - 1) % len(self.phases)
                phase_order = self.phases[offset:] + self.phases[:offset]
                execution_order.append({"repeat": repeat, "phases": list(phase_order)})
                for phase in phase_order:
                    row, action = self._run_repeat(
                        phase, repeat, config, run_dir
                    )
                    rows.append(row)
                    evidence[phase].append(action)
        finally:
            self._cleanup()

        summary = build_redis_summary(
            config, environment, rows, evidence, execution_order
        )
        write_json(run_dir / "summary.json", summary)
        self._write_csv(run_dir / "summary.csv", rows)
        self._write_report(run_dir / "report.md", summary)
        return {"status": "ok", "run_dir": str(run_dir), "summary": summary}

    def _run_repeat(
        self,
        phase: str,
        repeat: int,
        config: RedisBenchmarkConfig,
        run_dir: Path,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._cleanup()
        stress: subprocess.Popen[str] | None = None
        context: AgentContext | None = None
        action: dict[str, Any] = {"status": "not_required"}
        try:
            if phase != "baseline":
                stress = start_stress(
                    config.stress_cpu, config.duration + config.warmup + 30
                )
                time.sleep(config.warmup)
            if phase == "schedx":
                context, action = self._apply_agent()

            # Measure background progress over the client interval only. Agent
            # planning time is deliberately outside this paired window.
            stress_pids = process_ids("stress-ng", "stress-ng-cpu")
            ticks_before = cpu_ticks(stress_pids)

            command = [
                "redis-benchmark",
                "-h",
                config.host,
                "-p",
                str(config.port),
                "-n",
                str(config.requests),
                "-c",
                str(config.connections),
                "--threads",
                str(config.threads),
                "--precision",
                "3",
                "-t",
                "get",
            ]
            started = time.perf_counter()
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=max(config.duration * 5, 60),
            )
            wall_seconds = time.perf_counter() - started
            ticks_after = cpu_ticks(stress_pids)
            raw = completed.stdout + completed.stderr
            (run_dir / f"{phase}_redis_repeat{repeat}.txt").write_text(
                raw, encoding="utf-8"
            )
            metrics = parse_redis_benchmark_output(completed.stdout)
            return (
                {
                    "phase": phase,
                    "repeat": repeat,
                    **metrics,
                    "wall_seconds": round(wall_seconds, 4),
                    "background_cpu_ticks": max(ticks_after - ticks_before, 0),
                    "returncode": completed.returncode,
                    "notes": (
                        ""
                        if completed.returncode == 0 and metrics
                        else "redis_benchmark_failed_or_unparsed"
                    ),
                },
                action,
            )
        finally:
            if context is not None:
                RollbackSkill().run(context)
            stop_process(stress)
            self._cleanup()

    @staticmethod
    def _apply_agent() -> tuple[AgentContext, dict[str, Any]]:
        context = AgentContext(dry_run=False)
        context.data.update(
            {
                "interval": 0.2,
                "top": 80,
                "mode": "latency_first",
                "target": "redis",
            }
        )
        captured = io.StringIO()
        with redirect_stdout(captured):
            report = AgentLoop(context, max_iterations=16).run(initial_phase="probe")
        return context, {
            "status": report.get("final_status"),
            "agent_decision": context.data.get("agent_decision"),
            "classification": context.data.get("classification"),
            "scx_results": context.data.get("scx_results"),
            "execution_results": context.data.get("execution_results"),
            "ebpf": {
                "load": context.data.get("ebpf_load_results", {}),
                "attach": context.data.get("ebpf_attach_results", {}),
                "policies": context.data.get("ebpf_policy_results", []),
                "stats": context.data.get("ebpf_stats"),
            },
            "log": captured.getvalue(),
        }

    @staticmethod
    def _redis_ping(config: RedisBenchmarkConfig) -> str:
        try:
            completed = subprocess.run(
                ["redis-cli", "-h", config.host, "-p", str(config.port), "ping"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return completed.stdout.strip()

    @staticmethod
    def _cleanup() -> None:
        cleanup_stress()
        RollbackSkill().run(AgentContext(dry_run=False))

    @staticmethod
    def _validate(config: RedisBenchmarkConfig) -> None:
        if min(
            config.port,
            config.duration,
            config.connections,
            config.threads,
            config.repeats,
            config.stress_cpu,
        ) < 1:
            raise ValueError("Redis benchmark numeric options must be positive")
        if not 0.0 <= config.minimum_background_retention_percent <= 100.0:
            raise ValueError("minimum background retention must be in [0, 100]")

    @staticmethod
    def _fail(run_dir: Path, error: str) -> dict[str, Any]:
        result = {"status": "failed", "error": error, "run_dir": str(run_dir)}
        write_json(run_dir / "summary.json", result)
        return result

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        fields = [
            "phase",
            "repeat",
            "requests_per_sec",
            "latency_avg_ms",
            "p50_ms",
            "p95_ms",
            "p99_ms",
            "latency_max_ms",
            "wall_seconds",
            "background_cpu_ticks",
            "notes",
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fields})

    @staticmethod
    def _write_report(path: Path, summary: dict[str, Any]) -> None:
        lines = [
            "# SchedX-Agent Redis Mixed Workload Report",
            "",
            "This fixed-work benchmark rotates phase order across repeats to reduce order bias.",
            "Each row reports a small-sample Student-t 95% confidence interval.",
            "",
            "| Phase | Mean QPS | 95% CI | Mean P99 (ms) | 95% CI | Background retention | Valid |",
            "| --- | ---: | --- | ---: | --- | ---: | --- |",
        ]
        for phase in RedisMixedBenchmark.phases:
            item = summary["phases"][phase]
            lines.append(
                f"| {phase} | {_fmt(item['requests_per_sec']['mean'])} | "
                f"{_ci(item['requests_per_sec']['ci95'])} | "
                f"{_fmt(item['p99_ms']['mean'])} | {_ci(item['p99_ms']['ci95'])} | "
                f"{_pct(item.get('background_retention_percent'))} | "
                f"{item.get('valid_for_claims')} |"
            )
        lines.extend(
            [
                "",
                "## Result",
                "",
                f"- Interference QPS drop: {_pct(summary.get('interference_qps_drop_percent'))}",
                f"- Agent QPS recovery: {_pct(summary.get('schedx_qps_gain_vs_interference_percent'))}",
                f"- Agent P99 reduction: {_pct(summary.get('schedx_p99_reduction_vs_interference_percent'))}",
                f"- Fairness floor: {_pct(summary.get('minimum_background_retention_percent'))}",
                f"- Result valid for claims: {summary['phases']['schedx'].get('valid_for_claims')}",
                "",
                "## Agent Evidence",
                "",
                "The SchedX phase invokes the existing latency-first AgentLoop for Redis;",
                "the saved action evidence includes classification, scx/cgroup execution, eBPF evidence, and cleanup.",
            ]
        )
        path.write_text("\n".join(lines), encoding="utf-8")


def build_redis_summary(
    config: RedisBenchmarkConfig,
    environment: dict[str, Any],
    rows: list[dict[str, Any]],
    evidence: dict[str, list[dict[str, Any]]],
    execution_order: list[dict[str, Any]],
) -> dict[str, Any]:
    phase_rows = {
        phase: [row for row in rows if row["phase"] == phase]
        for phase in RedisMixedBenchmark.phases
    }
    interference_ticks = mean(phase_rows["interference"], "background_cpu_ticks")
    phases: dict[str, dict[str, Any]] = {}
    for phase in RedisMixedBenchmark.phases:
        current = phase_rows[phase]
        background_ticks = mean(current, "background_cpu_ticks")
        retention = (
            round(float(background_ticks) / float(interference_ticks) * 100.0, 4)
            if phase != "baseline"
            and interference_ticks not in (None, 0)
            and background_ticks is not None
            else None
        )
        phases[phase] = {
            "requests_per_sec": describe(current, "requests_per_sec"),
            "latency_avg_ms": describe(current, "latency_avg_ms"),
            "p99_ms": describe(current, "p99_ms"),
            "wall_seconds": describe(current, "wall_seconds"),
            "mean_background_cpu_ticks": background_ticks,
            "background_retention_percent": retention,
        }

    baseline_qps = phases["baseline"]["requests_per_sec"]["mean"]
    interference_qps = phases["interference"]["requests_per_sec"]["mean"]
    schedx_qps = phases["schedx"]["requests_per_sec"]["mean"]
    interference_p99 = phases["interference"]["p99_ms"]["mean"]
    schedx_p99 = phases["schedx"]["p99_ms"]["mean"]
    interference_drop = percent_drop(baseline_qps, interference_qps)
    qps_gain = percent_gain(interference_qps, schedx_qps)
    p99_drop = percent_drop(interference_p99, schedx_p99)
    schedx = phases["schedx"]
    schedx["valid_for_claims"] = bool(
        interference_drop is not None
        and interference_drop >= 2.0
        and qps_gain is not None
        and qps_gain > 0.0
        and (schedx.get("background_retention_percent") or 0.0)
        >= config.minimum_background_retention_percent
    )
    phases["baseline"]["valid_for_claims"] = True
    phases["interference"]["valid_for_claims"] = True
    return {
        "status": "ok",
        "benchmark": "redis-mixed-workload",
        "config": {**asdict(config), "output": str(config.output)},
        "environment": {
            "kernel": environment.get("uname", environment.get("kernel")),
            "cpu_count": environment.get("cpu_count"),
            "cgroup_v2": environment.get("cgroup_v2"),
            "sched_ext_available": environment.get("sched_ext_available"),
            "redis_status": environment.get("redis_status"),
        },
        "execution_order": execution_order,
        "phases": phases,
        "interference_qps_drop_percent": interference_drop,
        "schedx_qps_gain_vs_interference_percent": qps_gain,
        "schedx_p99_reduction_vs_interference_percent": p99_drop,
        "minimum_background_retention_percent": config.minimum_background_retention_percent,
        "action_evidence": evidence,
        "cleanup": {
            "stress_processes": process_ids("stress-ng", "stress-ng-cpu"),
            "cgroup_base_exists": Path("/sys/fs/cgroup/schedx").exists(),
            "ebpf_base_exists": Path("/sys/fs/bpf/schedx").exists(),
            "sched_ext_state": ScxController().state(),
        },
    }


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2f}"


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2f}%"


def _ci(value: Any) -> str:
    if not value:
        return "n/a"
    return f"[{float(value[0]):.2f}, {float(value[1]):.2f}]"
