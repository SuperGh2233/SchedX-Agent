from __future__ import annotations

import csv
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from schedx.benchmark.common import (
    cleanup_stress,
    cpu_ticks,
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


SCX_COMPARISON_SCHEDULERS = ("scx_simple", "scx_agent")
KNOWN_SCHEDULERS = frozenset(
    {"scx_simple", "scx_qmap", "scx_flatcg", "scx_agent"}
)


@dataclass
class ScxComparisonConfig:
    schedulers: tuple[str, ...] = SCX_COMPARISON_SCHEDULERS
    url: str = "http://127.0.0.1/"
    duration: int = 20
    connections: int = 64
    threads: int = 4
    repeats: int = 5
    stress_cpu: int = 4
    warmup: int = 3
    minimum_background_retention_percent: float = 25.0
    output: Path = Path("results/scx-compare")


def parse_scheduler_list(value: str | Sequence[str] | None) -> tuple[str, ...]:
    """Parse the explicit scx list; default is implicit and never repeated."""
    if value is None:
        names = list(SCX_COMPARISON_SCHEDULERS)
    elif isinstance(value, str):
        names = [item.strip() for item in value.split(",") if item.strip()]
    else:
        names = [str(item).strip() for item in value if str(item).strip()]
    if not names:
        raise ValueError("at least one scx scheduler is required")
    unknown = sorted(set(names) - KNOWN_SCHEDULERS - {"default"})
    if unknown:
        raise ValueError(f"scheduler is not allowlisted: {', '.join(unknown)}")
    result: list[str] = ["default"]
    for name in names:
        if name != "default" and name not in result:
            result.append(name)
    return tuple(result)


def run_scx_comparison(config: ScxComparisonConfig) -> dict[str, Any]:
    validate_config(config)
    schedulers = parse_scheduler_list(config.schedulers)
    run_dir = config.output / time.strftime("%Y-%m-%d_%H-%M-%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    environment = collect_environment()
    environment["requested_schedulers"] = list(schedulers)
    environment["scheduler_binaries"] = {
        name: shutil.which(name) for name in schedulers if name != "default"
    }
    write_json(run_dir / "env.json", environment)

    if not environment.get("wrk_available"):
        return _failed_result(run_dir, environment, config, "wrk not found")
    if not environment.get("stress_ng_available"):
        return _failed_result(run_dir, environment, config, "stress-ng not found")
    if ScxController(dry_run=False).state() == "enabled":
        return _failed_result(
            run_dir,
            environment,
            config,
            "sched_ext is already enabled; stop the existing scheduler first",
        )

    phases: dict[str, dict[str, Any]] = {}
    try:
        for scheduler in schedulers:
            phases[scheduler] = _run_scheduler_phase(scheduler, config, run_dir)
            write_json(run_dir / f"{scheduler}_evidence.json", phases[scheduler])
    finally:
        cleanup = _cleanup()

    summary = build_summary(config, environment, phases, cleanup)
    successful_scx = [
        name for name, phase in phases.items()
        if name != "default" and phase.get("status") == "ok"
    ]
    status = (
        "ok"
        if phases.get("default", {}).get("status") == "ok" and successful_scx
        else "failed"
    )
    summary["status"] = status
    _write_csv(run_dir / "summary.csv", phases)
    _write_report(run_dir / "report.md", summary)
    write_json(run_dir / "summary.json", summary)
    return {"status": status, "run_dir": str(run_dir), "summary": summary}


def validate_config(config: ScxComparisonConfig) -> None:
    if min(config.duration, config.connections, config.threads, config.repeats, config.stress_cpu) < 1:
        raise ValueError("duration, connections, threads, repeats, and stress_cpu must be positive")
    if not 0.0 <= config.minimum_background_retention_percent <= 100.0:
        raise ValueError("minimum background retention must be in [0, 100]")


def _run_scheduler_phase(
    scheduler: str, config: ScxComparisonConfig, run_dir: Path
) -> dict[str, Any]:
    binary = None if scheduler == "default" else shutil.which(scheduler)
    if scheduler != "default" and not binary:
        result = {
            "scheduler": scheduler,
            "status": "skipped",
            "reason": "scheduler_not_found",
            "policy_capability": _capability(scheduler),
            "rows": [],
            "background_cpu_ticks": None,
            "scheduler_stats": None,
        }
        (run_dir / f"{scheduler}_scheduler.log").write_text(
            json.dumps(
                {
                    "event": "skip",
                    "scheduler": scheduler,
                    "reason": "scheduler_not_found",
                    "policy_capability": _capability(scheduler),
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return result

    stress = start_stress(config.stress_cpu, config.duration * config.repeats + config.warmup + 20)
    context: dict[str, Any] | None = None
    log_path = run_dir / f"{scheduler}_scheduler.log"
    log_entries: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    try:
        time.sleep(config.warmup)
        context = _start_scheduler_context(scheduler, binary, log_path, log_entries)
        if not context.get("started", False):
            result = {
                "scheduler": scheduler,
                "status": "skipped",
                "reason": context.get("reason", "scheduler_start_failed"),
                "policy_capability": context.get("policy_capability", "unknown"),
                "rows": [],
                "background_cpu_ticks": None,
                "scheduler_stats": None,
            }
            return result

        if context.get("mode") == "policy":
            context["policy"] = _apply_agent_policy(context["controller"], log_entries)
        stress_pids = process_ids("stress-ng", "stress-ng-cpu")
        ticks_before = cpu_ticks(stress_pids)
        for repeat in range(1, config.repeats + 1):
            rows.append(_run_wrk(scheduler, repeat, config, run_dir))
        ticks_after = cpu_ticks(stress_pids)
        stats = _scheduler_stats(context)
        result = {
            "scheduler": scheduler,
            "status": "ok" if all(row.get("returncode") == 0 and row.get("metrics") for row in rows) else "failed",
            "policy_capability": context.get("policy_capability"),
            "rows": rows,
            "background_cpu_ticks": max(ticks_after - ticks_before, 0),
            "scheduler_stats": stats,
            "policy": context.get("policy", {}),
        }
        return result
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        return {
            "scheduler": scheduler,
            "status": "skipped",
            "reason": str(exc),
            "policy_capability": (context or {}).get("policy_capability", "unknown"),
            "rows": rows,
            "background_cpu_ticks": None,
            "scheduler_stats": None,
        }
    finally:
        if context is not None:
            _stop_scheduler_context(context, log_entries)
        stop_process(stress)
        cleanup_stress()
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in log_entries)
                + "\n"
            )


def _start_scheduler_context(
    scheduler: str,
    binary: str | None,
    log_path: Path,
    log_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    if scheduler == "default":
        log_entries.append({"event": "start", "scheduler": "default", "mode": "none"})
        return {"started": True, "mode": "none", "policy_capability": "none"}
    if binary is None:
        return {"started": False, "reason": "scheduler_not_found"}
    if scheduler == "scx_agent":
        controller = ScxController(dry_run=False)
        started = controller.start_scheduler("scx_agent")
        log_entries.append({"event": "start", "command": [binary], "started": started, "mode": "policy"})
        return {
            "started": bool(started),
            "mode": "policy",
            "policy_capability": "task_policy_and_fairness",
            "controller": controller,
        }

    # The other allowlisted schedulers have no shared task-policy protocol.
    output_handle = log_path.open("a", encoding="utf-8")
    process = subprocess.Popen(
        [binary],
        stdin=subprocess.DEVNULL,
        stdout=output_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    time.sleep(0.2)
    if process.poll() is not None:
        output_handle.close()
        log_entries.append(
            {"event": "start", "command": [binary], "started": False}
        )
        return {
            "started": False,
            "reason": "scheduler_exited_during_start",
            "mode": "lifecycle",
            "policy_capability": "lifecycle_only",
            "process": process,
        }
    log_entries.append({"event": "start", "command": [binary], "started": True, "mode": "lifecycle"})
    return {
        "started": True,
        "mode": "lifecycle",
        "policy_capability": "lifecycle_only",
        "process": process,
        "output_handle": output_handle,
    }


def _stop_scheduler_context(context: dict[str, Any], log_entries: list[dict[str, Any]]) -> None:
    controller = context.get("controller")
    if controller is not None:
        controller.stop_scheduler()
        log_entries.append({"event": "stop", "scheduler": "scx_agent", "stopped": True})
    process = context.get("process")
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, PermissionError):
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        log_entries.append(
            {"event": "stop", "scheduler": "lifecycle", "stopped": True}
        )
    output_handle = context.get("output_handle")
    if output_handle is not None:
        output_handle.close()


def _apply_agent_policy(controller: ScxController, log_entries: list[dict[str, Any]]) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    for pid in process_ids("nginx"):
        actions.append({"pid": pid, "class": "latency", "weight": 10000, "success": controller.set_task_policy(pid, SCX_CLASS_LATENCY, 10000)})
    for pid in process_ids("stress-ng", "stress-ng-cpu"):
        actions.append({"pid": pid, "class": "background", "weight": 100, "success": controller.set_task_policy(pid, SCX_CLASS_BACKGROUND, 100)})
    fairness = controller.set_fairness(SCX_FAIRNESS_BACKGROUND_DEFAULT, 0)
    policy = {"actions": actions, "fairness": fairness}
    log_entries.append({"event": "policy", "policy": policy})
    return policy


def _scheduler_stats(context: dict[str, Any]) -> dict[str, Any] | None:
    controller = context.get("controller")
    return controller.get_stats().to_dict() if controller is not None else None


def _capability(scheduler: str) -> str:
    return "task_policy_and_fairness" if scheduler == "scx_agent" else "lifecycle_only"


def _run_wrk(scheduler: str, repeat: int, config: ScxComparisonConfig, run_dir: Path) -> dict[str, Any]:
    command = [
        "wrk",
        f"-t{config.threads}",
        f"-c{config.connections}",
        f"-d{config.duration}s",
        "--latency",
        config.url,
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=config.duration + 20)
    raw = completed.stdout + completed.stderr
    (run_dir / f"{scheduler}_wrk_repeat{repeat}.txt").write_text(raw, encoding="utf-8")
    metrics = parse_wrk_output(raw)
    return {
        "scheduler": scheduler,
        "repeat": repeat,
        "returncode": completed.returncode,
        "metrics": metrics,
        **metrics,
        "notes": "" if completed.returncode == 0 and metrics else "wrk_failed_or_unparsed",
    }


def build_summary(
    config: ScxComparisonConfig,
    environment: dict[str, Any],
    phases: dict[str, dict[str, Any]],
    cleanup: dict[str, Any],
) -> dict[str, Any]:
    default_ticks = phases.get("default", {}).get("background_cpu_ticks")
    default_stats = aggregate_rows(phases.get("default", {}).get("rows", []))
    summaries: dict[str, dict[str, Any]] = {}
    for scheduler, phase in phases.items():
        stats = aggregate_rows(phase.get("rows", []))
        ticks = phase.get("background_cpu_ticks")
        retention = _percent(ticks, default_ticks)
        valid = phase.get("status") == "ok" and _rows_have_metrics(phase.get("rows", []))
        if scheduler != "default":
            valid = valid and retention is not None and retention >= config.minimum_background_retention_percent
        summaries[scheduler] = {
            "status": phase.get("status"),
            "reason": phase.get("reason", ""),
            "policy_capability": phase.get("policy_capability"),
            "statistics": stats,
            "background_cpu_ticks": ticks,
            "background_retention_percent": retention,
            "fairness": {
                "minimum_background_retention_percent": config.minimum_background_retention_percent,
                "valid_for_claims": valid,
            },
            "scheduler_stats": phase.get("scheduler_stats"),
        }
        if scheduler != "default":
            summaries[scheduler]["comparison_vs_default"] = {
                "rps_gain_percent": _percent_gain(
                    _stat(default_stats, "requests_per_sec", "mean"),
                    _stat(stats, "requests_per_sec", "mean"),
                ),
                "p99_reduction_percent": _percent_drop(
                    _stat(default_stats, "p99_ms", "mean"),
                    _stat(stats, "p99_ms", "mean"),
                ),
            }
    return {
        "schema_version": "1.0",
        "benchmark": "nginx-scx-comparison",
        "config": {**asdict(config), "schedulers": list(config.schedulers), "output": str(config.output)},
        "environment": {
            "kernel": environment.get("uname", environment.get("kernel")),
            "cpu_count": environment.get("cpu_count"),
            "cgroup_v2": environment.get("cgroup_v2"),
            "sched_ext_available": environment.get("sched_ext_available"),
        },
        "schedulers": summaries,
        "cleanup": cleanup,
    }


def aggregate_rows(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, float | None]]:
    result: dict[str, dict[str, float | None]] = {}
    for key in ("requests_per_sec", "latency_avg_ms", "p99_ms"):
        values = [float(row[key]) for row in rows if row.get(key) is not None]
        result[key] = {
            "mean": round(statistics.mean(values), 4) if values else None,
            "median": round(statistics.median(values), 4) if values else None,
            "stdev": round(statistics.stdev(values), 4) if len(values) > 1 else None,
        }
    return result


def _rows_have_metrics(rows: Sequence[dict[str, Any]]) -> bool:
    return bool(rows) and all(row.get("returncode") == 0 and row.get("metrics") for row in rows)


def _stat(stats: dict[str, Any], key: str, aggregate: str) -> float | None:
    value = stats.get(key, {}).get(aggregate)
    return float(value) if value is not None else None


def _percent(value: Any, baseline: Any) -> float | None:
    if value is None or baseline in (None, 0):
        return None
    return round(float(value) / float(baseline) * 100.0, 4)


def _percent_gain(before: Any, after: Any) -> float | None:
    if before in (None, 0) or after is None:
        return None
    return round((float(after) - float(before)) / float(before) * 100.0, 4)


def _percent_drop(before: Any, after: Any) -> float | None:
    if before in (None, 0) or after is None:
        return None
    return round((float(before) - float(after)) / float(before) * 100.0, 4)


def _cleanup() -> dict[str, Any]:
    cleanup_stress()
    rollback = _run_schedx_json(["rollback"])
    cleanup_stress()
    return {
        "rollback": rollback,
        "stress_stopped": not bool(process_ids("stress-ng", "stress-ng-cpu")),
        "sched_ext_disabled": ScxController(dry_run=False).state() == "disabled",
        "cgroup_clean": not Path("/sys/fs/cgroup/schedx").exists(),
    }


def _run_schedx_json(args: Sequence[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "schedx.main", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        data = {}
    return {"returncode": completed.returncode, "data": data, "stderr": completed.stderr[-2000:]}


def _failed_result(
    run_dir: Path,
    environment: dict[str, Any],
    config: ScxComparisonConfig,
    error: str,
) -> dict[str, Any]:
    cleanup = _cleanup()
    summary = {
        "schema_version": "1.0",
        "benchmark": "nginx-scx-comparison",
        "config": {**asdict(config), "schedulers": list(config.schedulers), "output": str(config.output)},
        "environment": environment,
        "status": "failed",
        "error": error,
        "schedulers": {},
        "cleanup": cleanup,
    }
    write_json(run_dir / "summary.json", summary)
    _write_csv(run_dir / "summary.csv", {})
    _write_report(run_dir / "report.md", summary)
    return {"status": "failed", "run_dir": str(run_dir), "error": error}


def _write_csv(path: Path, phases: dict[str, dict[str, Any]]) -> None:
    fields = [
        "scheduler",
        "capability",
        "repeat",
        "status",
        "requests_per_sec",
        "latency_avg_ms",
        "p99_ms",
        "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for scheduler, phase in phases.items():
            if not phase.get("rows"):
                writer.writerow(
                    {
                        "scheduler": scheduler,
                        "capability": phase.get("policy_capability", ""),
                        "status": phase.get("status"),
                        "notes": phase.get("reason", ""),
                    }
                )
            for row in phase.get("rows", []):
                writer.writerow({
                    "scheduler": scheduler,
                    "capability": phase.get("policy_capability", ""),
                    "repeat": row.get("repeat"),
                    "status": phase.get("status"),
                    "requests_per_sec": row.get("requests_per_sec", ""),
                    "latency_avg_ms": row.get("latency_avg_ms", ""),
                    "p99_ms": row.get("p99_ms", ""),
                    "notes": row.get("notes", ""),
                })


def _write_report(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# SchedX-Agent Nginx sched_ext Scheduler Comparison",
        "",
        "The implicit baseline is the default Linux scheduler. Requested scx schedulers are measured under the same nginx and stress-ng workload.",
        "",
        "| Scheduler | Capability | Status | Mean RPS | Median RPS | RPS stdev | Mean P99 (ms) | Retention | Valid |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for scheduler, item in summary.get("schedulers", {}).items():
        rps = item.get("statistics", {}).get("requests_per_sec", {})
        p99 = item.get("statistics", {}).get("p99_ms", {})
        fairness = item.get("fairness", {})
        retention = fairness.get("background_retention_percent")
        if retention is None:
            retention = item.get("background_retention_percent")
        lines.append(
            f"| {scheduler} | {item.get('policy_capability', 'n/a')} | {item.get('status', 'n/a')} | "
            f"{_fmt(rps.get('mean'))} | {_fmt(rps.get('median'))} | "
            f"{_fmt(rps.get('stdev'))} | {_fmt(p99.get('mean'))} | {_pct(retention)} | "
            f"{fairness.get('valid_for_claims', False)} |"
        )
    lines.extend(["", "## Cleanup", "", f"```json\n{json.dumps(summary.get('cleanup', {}), indent=2, ensure_ascii=False)}\n```", ""])
    if summary.get("error"):
        lines.append(f"Error: {summary['error']}")
    path.write_text("\n".join(lines), encoding="utf-8")


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.4f}"


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2f}%"
