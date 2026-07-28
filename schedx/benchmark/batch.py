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
    mean,
    percent_drop,
    percent_gain,
    process_ids,
    start_stress,
    stop_process,
    write_json,
)
from schedx.benchmark.nginx import collect_environment
from schedx.benchmark.sysbench_parser import parse_sysbench_output
from schedx.controllers.scx_controller import ScxController
from schedx.skills.rollback_skill import RollbackSkill


@dataclass
class BatchThroughputConfig:
    duration: int = 10
    threads: int = 4
    repeats: int = 3
    stress_cpu: int = 4
    warmup: int = 2
    output: Path = Path("results/batch-throughput")


class BatchThroughputBenchmark:
    phases = ("baseline", "interference", "schedx")

    def run(self, config: BatchThroughputConfig) -> dict[str, Any]:
        self._validate(config)
        run_dir = config.output / time.strftime("%Y-%m-%d_%H-%M-%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        environment = collect_environment()
        write_json(run_dir / "env.json", environment)
        missing = [tool for tool in ("sysbench", "stress-ng") if not shutil.which(tool)]
        if missing:
            result = {
                "status": "failed",
                "error": f"missing required tools: {', '.join(missing)}",
                "run_dir": str(run_dir),
            }
            write_json(run_dir / "summary.json", result)
            return result
        if ScxController().state() == "enabled":
            result = {
                "status": "failed",
                "error": "sched_ext is already enabled; stop it before a clean batch comparison",
                "run_dir": str(run_dir),
            }
            write_json(run_dir / "summary.json", result)
            return result

        rows: list[dict[str, Any]] = []
        evidence: dict[str, list[dict[str, Any]]] = {phase: [] for phase in self.phases}
        try:
            for phase in self.phases:
                for repeat in range(1, config.repeats + 1):
                    row, action = self._run_repeat(phase, repeat, config, run_dir)
                    rows.append(row)
                    evidence[phase].append(action)
        finally:
            self._cleanup()

        summary = build_batch_summary(config, environment, rows, evidence)
        write_json(run_dir / "summary.json", summary)
        self._write_csv(run_dir / "summary.csv", rows)
        self._write_report(run_dir / "report.md", summary)
        return {"status": "ok", "run_dir": str(run_dir), "summary": summary}

    def _run_repeat(
        self,
        phase: str,
        repeat: int,
        config: BatchThroughputConfig,
        run_dir: Path,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._cleanup()
        stress: subprocess.Popen[str] | None = None
        batch: subprocess.Popen[str] | None = None
        context: AgentContext | None = None
        action: dict[str, Any] = {"status": "not_required"}
        try:
            if phase != "baseline":
                stress = start_stress(
                    config.stress_cpu, config.duration + config.warmup + 20
                )
                time.sleep(config.warmup)
            stress_pids = process_ids("stress-ng", "stress-ng-cpu")
            ticks_before = cpu_ticks(stress_pids)
            command = [
                "sysbench",
                "cpu",
                f"--threads={config.threads}",
                f"--time={config.duration}",
                "run",
            ]
            batch = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            if phase == "schedx":
                time.sleep(min(1.0, max(config.duration / 5.0, 0.2)))
                context, action = self._apply_agent()
            stdout, stderr = batch.communicate(timeout=config.duration + 30)
            ticks_after = cpu_ticks(stress_pids)
            raw = stdout + stderr
            (run_dir / f"{phase}_sysbench_repeat{repeat}.txt").write_text(
                raw, encoding="utf-8"
            )
            metrics = parse_sysbench_output(stdout)
            return (
                {
                    "phase": phase,
                    "repeat": repeat,
                    **metrics,
                    "background_cpu_ticks": max(ticks_after - ticks_before, 0),
                    "returncode": batch.returncode,
                    "notes": (
                        ""
                        if batch.returncode == 0 and metrics
                        else "sysbench_failed_or_unparsed"
                    ),
                },
                action,
            )
        finally:
            if context is not None:
                RollbackSkill().run(context)
            if batch is not None and batch.poll() is None:
                stop_process(batch)
            stop_process(stress)
            RollbackSkill().run(AgentContext(dry_run=False))
            cleanup_stress()

    @staticmethod
    def _apply_agent() -> tuple[AgentContext, dict[str, Any]]:
        context = AgentContext(dry_run=False)
        context.data.update(
            {
                "interval": 0.2,
                "top": 80,
                "mode": "throughput_first",
                "target": "sysbench",
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
            "log": captured.getvalue(),
        }

    @staticmethod
    def _cleanup() -> None:
        cleanup_stress()
        RollbackSkill().run(AgentContext(dry_run=False))

    @staticmethod
    def _validate(config: BatchThroughputConfig) -> None:
        if min(config.duration, config.threads, config.repeats, config.stress_cpu) < 1:
            raise ValueError(
                "duration, threads, repeats, and stress_cpu must be positive"
            )

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        fields = [
            "phase",
            "repeat",
            "events_per_second",
            "events",
            "elapsed_seconds",
            "latency_p95_ms",
            "background_cpu_ticks",
            "notes",
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fields})

    @staticmethod
    def _write_report(path: Path, summary: dict[str, Any]) -> None:
        lines = [
            "# SchedX-Agent Batch Throughput Report",
            "",
            "| Phase | Events/s | P95 latency (ms) | Change vs interference |",
            "| --- | ---: | ---: | ---: |",
        ]
        for phase in BatchThroughputBenchmark.phases:
            item = summary["phases"][phase]
            lines.append(
                f"| {phase} | {_fmt(item.get('mean_events_per_second'))} | "
                f"{_fmt(item.get('mean_latency_p95_ms'))} | "
                f"{_pct(item.get('throughput_gain_vs_interference_percent'))} |"
            )
        lines.extend(
            [
                "",
                "The SchedX phase launches sysbench first, identifies the live batch workload,",
                "and applies the throughput expert while CPU interference is active.",
            ]
        )
        path.write_text("\n".join(lines), encoding="utf-8")


def build_batch_summary(
    config: BatchThroughputConfig,
    environment: dict[str, Any],
    rows: list[dict[str, Any]],
    evidence: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    phases: dict[str, dict[str, Any]] = {}
    interference_rows = [row for row in rows if row["phase"] == "interference"]
    interference_eps = mean(interference_rows, "events_per_second")
    baseline_rows = [row for row in rows if row["phase"] == "baseline"]
    baseline_eps = mean(baseline_rows, "events_per_second")
    for phase in BatchThroughputBenchmark.phases:
        phase_rows = [row for row in rows if row["phase"] == phase]
        eps = mean(phase_rows, "events_per_second")
        phases[phase] = {
            "mean_events_per_second": eps,
            "mean_elapsed_seconds": mean(phase_rows, "elapsed_seconds"),
            "mean_latency_p95_ms": mean(phase_rows, "latency_p95_ms"),
            "throughput_gain_vs_interference_percent": percent_gain(
                interference_eps, eps
            ),
            "throughput_drop_vs_baseline_percent": percent_drop(baseline_eps, eps),
        }
    return {
        "status": "ok",
        "benchmark": "batch-throughput",
        "config": {**asdict(config), "output": str(config.output)},
        "environment": {
            "kernel": environment.get("uname", environment.get("kernel")),
            "cpu_count": environment.get("cpu_count"),
            "cgroup_v2": environment.get("cgroup_v2"),
            "sched_ext_available": environment.get("sched_ext_available"),
        },
        "phases": phases,
        "action_evidence": evidence,
        "cleanup": {
            "stress_processes": process_ids("stress-ng", "stress-ng-cpu"),
            "cgroup_base_exists": Path("/sys/fs/cgroup/schedx").exists(),
            "sched_ext_state": ScxController().state(),
        },
    }


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2f}"


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2f}%"
