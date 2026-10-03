from __future__ import annotations

from schedx.controllers.scx_controller import SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL

import csv
import io
import json
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
from schedx.benchmark.wrk_parser import parse_wrk_output
from schedx.controllers.scx_controller import (
    SCX_CLASS_BACKGROUND,
    SCX_CLASS_LATENCY,
    SCX_FAIRNESS_BACKGROUND_DEFAULT,
    ScxController,
)
from schedx.skills.act_skill import ActSkill
from schedx.skills.analyze_skill import AnalyzeSkill
from schedx.skills.policy_skill import PolicySkill
from schedx.skills.probe_skill import ProbeSkill
from schedx.skills.rollback_skill import RollbackSkill


@dataclass
class NginxAblationConfig:
    url: str = "http://127.0.0.1/"
    duration: int = 10
    connections: int = 64
    threads: int = 4
    repeats: int = 3
    stress_cpu: int = 4
    warmup: int = 2
    minimum_background_retention_percent: float = 25.0
    output: Path = Path("results/nginx-ablation")


class NginxAblationBenchmark:
    phases = ("default", "cgroup_only", "scx_only", "agent_combined")

    def run(self, config: NginxAblationConfig) -> dict[str, Any]:
        self._validate(config)
        run_dir = config.output / time.strftime("%Y-%m-%d_%H-%M-%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        environment = collect_environment()
        write_json(run_dir / "env.json", environment)
        missing = [tool for tool in ("wrk", "stress-ng") if not shutil.which(tool)]
        if missing:
            result = {
                "status": "failed",
                "error": f"missing required tools: {', '.join(missing)}",
                "run_dir": str(run_dir),
            }
            write_json(run_dir / "summary.json", result)
            return result

        ctl = ScxController(dry_run=False)
        if ctl.state() == "enabled":
            result = {
                "status": "failed",
                "error": "sched_ext is already enabled; stop the existing scheduler before a clean ablation",
                "run_dir": str(run_dir),
            }
            write_json(run_dir / "summary.json", result)
            return result

        phase_results: dict[str, dict[str, Any]] = {}
        try:
            for phase in self.phases:
                phase_results[phase] = self._run_phase(phase, config, run_dir)
        finally:
            self._global_cleanup()

        summary = build_ablation_summary(config, environment, phase_results)
        write_json(run_dir / "summary.json", summary)
        self._write_csv(run_dir / "summary.csv", phase_results)
        self._write_report(run_dir / "report.md", summary)
        return {"status": "ok", "run_dir": str(run_dir), "summary": summary}

    def _run_phase(
        self, phase: str, config: NginxAblationConfig, run_dir: Path
    ) -> dict[str, Any]:
        self._global_cleanup()
        if phase in {"default", "cgroup_only"} and ScxController().state() == "enabled":
            raise RuntimeError(f"{phase} requires sched_ext to be disabled")

        stress = start_stress(
            config.stress_cpu,
            config.duration * max(config.repeats, 1) + config.warmup + 20,
        )
        context: AgentContext | None = None
        standalone: ScxController | None = None
        action_evidence: dict[str, Any] = {"phase": phase, "status": "not_required"}
        rows: list[dict[str, Any]] = []
        stats: dict[str, Any] = {}
        try:
            time.sleep(config.warmup)
            if phase == "cgroup_only":
                context, action_evidence = self._apply_cgroup_only()
            elif phase == "scx_only":
                standalone, action_evidence = self._apply_scx_only()
            elif phase == "agent_combined":
                context, action_evidence = self._apply_agent()

            stress_pids = process_ids("stress-ng", "stress-ng-cpu")
            ticks_before = cpu_ticks(stress_pids)
            for repeat in range(1, config.repeats + 1):
                row = self._run_wrk(phase, repeat, config, run_dir)
                rows.append(row)
            ticks_after = cpu_ticks(stress_pids)
            if phase in {"scx_only", "agent_combined"}:
                stats = ScxController().get_stats().to_dict()
            result = {
                "phase": phase,
                "rows": rows,
                "background_cpu_ticks": max(ticks_after - ticks_before, 0),
                "action_evidence": action_evidence,
                "sched_ext_stats": stats,
            }
            write_json(run_dir / f"{phase}_evidence.json", result)
            return result
        finally:
            if context is not None:
                RollbackSkill().run(context)
            if standalone is not None:
                standalone.stop_scheduler()
            stop_process(stress)
            RollbackSkill().run(AgentContext(dry_run=False))
            cleanup_stress()

    def _apply_cgroup_only(self) -> tuple[AgentContext, dict[str, Any]]:
        context = AgentContext(dry_run=False)
        context.data.update({"interval": 0.2, "top": 80})
        probe = ProbeSkill().run(context)
        analyze = AnalyzeSkill().run(context)
        context.data.update({"mode": "isolate_background", "target": "stress-ng"})
        policy = PolicySkill().run(context)
        act = ActSkill().run(context)
        return context, {
            "status": (
                "ok" if all(r.ok for r in (probe, analyze, policy, act)) else "failed"
            ),
            "classification": context.data.get("classification", {}),
            "actions": [asdict(action) for action in context.data.get("actions", [])],
            "execution_results": context.data.get("execution_results", []),
        }

    def _apply_scx_only(self) -> tuple[ScxController, dict[str, Any]]:
        controller = ScxController(dry_run=False)
        controller.start_scheduler("scx_agent")
        latency_pids = process_ids("nginx")
        background_pids = process_ids("stress-ng", "stress-ng-cpu")
        applied = []
        for pid in latency_pids:
            applied.append(
                {
                    "pid": pid,
                    "class": "latency",
                    "weight": 10000,
                    "success": controller.set_task_policy(
                        pid, SCX_CLASS_LATENCY, 10000
                    ),
                }
            )
        for pid in background_pids:
            applied.append(
                {
                    "pid": pid,
                    "class": "background",
                    "weight": 100,
                    "success": controller.set_task_policy(
                        pid, SCX_CLASS_BACKGROUND, 100
                    ),
                }
            )
        controller.set_fairness(
            background_interval=SCX_FAIRNESS_BACKGROUND_DEFAULT,
            default_interval=SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL,
        )
        return controller, {"status": "ok", "policies": applied}

    def _apply_agent(self) -> tuple[AgentContext, dict[str, Any]]:
        context = AgentContext(dry_run=False)
        context.data.update({"interval": 0.2, "top": 80})
        captured = io.StringIO()
        with redirect_stdout(captured):
            report = AgentLoop(context, max_iterations=16).run(initial_phase="probe")
        return context, {
            "status": report.get("final_status"),
            "agent_decision": context.data.get("agent_decision"),
            "policy_route": context.data.get("policy_route"),
            "scx_results": context.data.get("scx_results"),
            "execution_results": context.data.get("execution_results"),
            "log": captured.getvalue(),
        }

    @staticmethod
    def _run_wrk(
        phase: str,
        repeat: int,
        config: NginxAblationConfig,
        run_dir: Path,
    ) -> dict[str, Any]:
        command = [
            "wrk",
            f"-t{config.threads}",
            f"-c{config.connections}",
            f"-d{config.duration}s",
            "--latency",
            config.url,
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=config.duration + 20,
        )
        raw = completed.stdout + completed.stderr
        (run_dir / f"{phase}_wrk_repeat{repeat}.txt").write_text(raw, encoding="utf-8")
        metrics = parse_wrk_output(completed.stdout)
        return {
            "phase": phase,
            "repeat": repeat,
            **metrics,
            "returncode": completed.returncode,
            "notes": (
                ""
                if completed.returncode == 0 and metrics
                else "wrk_failed_or_unparsed"
            ),
        }

    @staticmethod
    def _global_cleanup() -> None:
        cleanup_stress()
        RollbackSkill().run(AgentContext(dry_run=False))

    @staticmethod
    def _validate(config: NginxAblationConfig) -> None:
        if (
            min(
                config.duration,
                config.connections,
                config.threads,
                config.repeats,
                config.stress_cpu,
            )
            < 1
        ):
            raise ValueError(
                "duration, connections, threads, repeats, and stress_cpu must be positive"
            )
        if not 0.0 <= config.minimum_background_retention_percent <= 100.0:
            raise ValueError("minimum background retention must be in [0, 100]")

    @staticmethod
    def _write_csv(path: Path, phase_results: dict[str, dict[str, Any]]) -> None:
        fields = [
            "phase",
            "repeat",
            "requests_per_sec",
            "latency_avg_ms",
            "p99_ms",
            "notes",
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for phase in NginxAblationBenchmark.phases:
                for row in phase_results[phase]["rows"]:
                    writer.writerow({key: row.get(key, "") for key in fields})

    @staticmethod
    def _write_report(path: Path, summary: dict[str, Any]) -> None:
        lines = [
            "# SchedX-Agent Nginx Ablation Report",
            "",
            "| Phase | Mean RPS | Mean P99 (ms) | RPS gain | P99 reduction | Background retention | Valid |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        for phase in NginxAblationBenchmark.phases:
            item = summary["phases"][phase]
            lines.append(
                f"| {phase} | {_fmt(item.get('mean_requests_per_sec'))} | "
                f"{_fmt(item.get('mean_p99_ms'))} | {_pct(item.get('rps_gain_vs_default_percent'))} | "
                f"{_pct(item.get('p99_reduction_vs_default_percent'))} | "
                f"{_pct(item.get('background_retention_percent'))} | {item.get('valid_for_claims')} |"
            )
        lines.extend(
            [
                "",
                "The four phases isolate the contribution of cgroup v2, native sched_ext, and the combined Agent.",
                "Performance claims are valid only when the configured background-progress floor is met.",
            ]
        )
        path.write_text("\n".join(lines), encoding="utf-8")


def build_ablation_summary(
    config: NginxAblationConfig,
    environment: dict[str, Any],
    phase_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    default_rows = phase_results["default"]["rows"]
    default_rps = mean(default_rows, "requests_per_sec")
    default_p99 = mean(default_rows, "p99_ms")
    default_ticks = int(phase_results["default"].get("background_cpu_ticks", 0))
    phases: dict[str, dict[str, Any]] = {}
    for phase in NginxAblationBenchmark.phases:
        result = phase_results[phase]
        rps = mean(result["rows"], "requests_per_sec")
        p99 = mean(result["rows"], "p99_ms")
        ticks = int(result.get("background_cpu_ticks", 0))
        retention = round(ticks / default_ticks * 100.0, 4) if default_ticks else None
        phases[phase] = {
            "mean_requests_per_sec": rps,
            "mean_p99_ms": p99,
            "background_cpu_ticks": ticks,
            "background_retention_percent": retention,
            "rps_gain_vs_default_percent": percent_gain(default_rps, rps),
            "p99_reduction_vs_default_percent": percent_drop(default_p99, p99),
            "valid_for_claims": (
                phase == "default"
                or retention is not None
                and retention >= config.minimum_background_retention_percent
            ),
        }
    return {
        "status": "ok",
        "benchmark": "nginx-ablation",
        "config": {**asdict(config), "output": str(config.output)},
        "environment": {
            "kernel": environment.get("uname", environment.get("kernel")),
            "cpu_count": environment.get("cpu_count"),
            "cgroup_v2": environment.get("cgroup_v2"),
            "sched_ext_available": environment.get("sched_ext_available"),
        },
        "phases": phases,
        "minimum_background_retention_percent": config.minimum_background_retention_percent,
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
