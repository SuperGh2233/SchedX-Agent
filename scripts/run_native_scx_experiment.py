#!/usr/bin/env python3
"""Compare the default Linux scheduler with SchedX native sched_ext."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schedx.benchmark.wrk_parser import parse_wrk_output
from schedx.controllers.scx_controller import (
    SCX_FAIRNESS_BACKGROUND_DEFAULT,
    SCX_CLASS_BACKGROUND,
    SCX_CLASS_LATENCY,
    ScxController,
)

DEFAULT_BACKGROUND_INTERVAL = SCX_FAIRNESS_BACKGROUND_DEFAULT
MIN_BACKGROUND_RETENTION_PERCENT = 25.0


def process_ids(name: str) -> list[int]:
    result = subprocess.run(
        ["pgrep", "-x", name], capture_output=True, text=True, check=False
    )
    return [int(value) for value in result.stdout.split() if value.isdigit()]


def cpu_ticks(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            total += int(fields[13]) + int(fields[14])
        except (FileNotFoundError, IndexError, ValueError):
            continue
    return total


def run_wrk(duration: int) -> dict[str, float | str]:
    completed = subprocess.run(
        ["wrk", "-t4", "-c64", f"-d{duration}s", "--latency", "http://127.0.0.1/"],
        capture_output=True,
        text=True,
        check=False,
    )
    return parse_wrk_output(completed.stdout)


def mean(rows: list[dict[str, float | str]], key: str) -> float:
    return statistics.mean(float(row[key]) for row in rows if key in row)


def percent_gain(before: float, after: float) -> float:
    return (after - before) / before * 100 if before else 0.0


def percent_drop(before: float, after: float) -> float:
    return (before - after) / before * 100 if before else 0.0


def background_retention_percent(default_ticks: int, native_ticks: int) -> float:
    return native_ticks / default_ticks * 100 if default_ticks else 0.0


def comparison_is_fair(
    default_ticks: int,
    native_ticks: int,
    minimum_percent: float = MIN_BACKGROUND_RETENTION_PERCENT,
) -> bool:
    return background_retention_percent(default_ticks, native_ticks) >= minimum_percent


def run_phase(
    duration: int,
    repeats: int,
    native: bool,
    background_interval: int = DEFAULT_BACKGROUND_INTERVAL,
) -> dict:
    ctl = ScxController(dry_run=False)
    stress = subprocess.Popen(
        ["stress-ng", "--cpu", str(os.cpu_count() or 4), "--timeout", f"{duration * repeats + 20}s"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2)
        if native:
            ctl.start_scheduler()
            for pid in process_ids("nginx"):
                ctl.set_task_policy(pid, SCX_CLASS_LATENCY, 10000)
            for pid in process_ids("stress-ng") + process_ids("stress-ng-cpu"):
                ctl.set_task_policy(pid, SCX_CLASS_BACKGROUND, 100)
            ctl.set_fairness(background_interval=background_interval, default_interval=0)
        stress_pids = process_ids("stress-ng") + process_ids("stress-ng-cpu")
        ticks_before = cpu_ticks(stress_pids)
        rows = [run_wrk(duration) for _ in range(repeats)]
        ticks_after = cpu_ticks(stress_pids)
        stats = ctl.get_stats().to_dict() if native else {}
        return {
            "rows": rows,
            "dispatch_stats": stats,
            "background_cpu_ticks": ticks_after - ticks_before,
        }
    finally:
        ctl.stop_scheduler()
        stress.terminate()
        try:
            stress.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stress.kill()
        subprocess.run(["pkill", "-f", "stress-ng"], check=False)


def main() -> None:
    duration = int(os.environ.get("SCHEDX_NATIVE_DURATION", "10"))
    repeats = int(os.environ.get("SCHEDX_NATIVE_REPEATS", "3"))
    background_interval = int(
        os.environ.get("SCHEDX_NATIVE_BACKGROUND_INTERVAL", str(DEFAULT_BACKGROUND_INTERVAL))
    )
    minimum_retention = float(
        os.environ.get(
            "SCHEDX_NATIVE_MIN_BACKGROUND_RETENTION",
            str(MIN_BACKGROUND_RETENTION_PERCENT),
        )
    )
    output = Path(os.environ.get("SCHEDX_NATIVE_OUTPUT", "results/native-scx"))
    output.mkdir(parents=True, exist_ok=True)

    default = run_phase(duration, repeats, native=False)
    native = run_phase(
        duration,
        repeats,
        native=True,
        background_interval=background_interval,
    )
    default_rps = mean(default["rows"], "requests_per_sec")
    native_rps = mean(native["rows"], "requests_per_sec")
    default_p99 = mean(default["rows"], "p99_ms")
    native_p99 = mean(native["rows"], "p99_ms")
    default_bg_ticks = default["background_cpu_ticks"]
    native_bg_ticks = native["background_cpu_ticks"]
    retention = background_retention_percent(default_bg_ticks, native_bg_ticks)
    valid_for_claims = comparison_is_fair(
        default_bg_ticks,
        native_bg_ticks,
        minimum_percent=minimum_retention,
    )
    result = {
        "kernel": subprocess.check_output(["uname", "-r"], text=True).strip(),
        "duration_seconds": duration,
        "repeats": repeats,
        "background_interval": background_interval,
        "minimum_background_retention_percent": minimum_retention,
        "default": default,
        "native_sched_ext": native,
        "comparison": {
            "rps_gain_percent": percent_gain(default_rps, native_rps),
            "p99_reduction_percent": percent_drop(default_p99, native_p99),
            "default_mean_rps": default_rps,
            "native_mean_rps": native_rps,
            "default_mean_p99_ms": default_p99,
            "native_mean_p99_ms": native_p99,
            "default_background_cpu_ticks": default_bg_ticks,
            "native_background_cpu_ticks": native_bg_ticks,
            "background_cpu_retention_percent": retention,
            "valid_for_performance_claims": valid_for_claims,
            "validity_reason": (
                "background_retention_floor_met"
                if valid_for_claims
                else "background_retention_below_floor"
            ),
        },
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    comparison = result["comparison"]
    report = [
        "# Native sched_ext Performance Comparison",
        "",
        f"- Kernel: `{result['kernel']}`",
        f"- Duration per repeat: {duration}s",
        f"- Repeats: {repeats}",
        f"- Background service interval: {background_interval}",
        f"- Default scheduler mean RPS: {default_rps:.2f}",
        f"- Native sched_ext mean RPS: {native_rps:.2f}",
        f"- RPS gain: {comparison['rps_gain_percent']:.2f}%",
        f"- Default scheduler mean P99: {default_p99:.2f} ms",
        f"- Native sched_ext mean P99: {native_p99:.2f} ms",
        f"- P99 reduction: {comparison['p99_reduction_percent']:.2f}%",
        f"- Default background CPU ticks: {default_bg_ticks}",
        f"- Native background CPU ticks: {native_bg_ticks}",
        f"- Background CPU retention: {comparison['background_cpu_retention_percent']:.2f}%",
        f"- Minimum accepted background retention: {minimum_retention:.2f}%",
        f"- Valid for performance claims: {valid_for_claims}",
        f"- Validity reason: `{comparison['validity_reason']}`",
        f"- Native dispatch stats: `{json.dumps(native['dispatch_stats'])}`",
    ]
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(result["comparison"], indent=2))


if __name__ == "__main__":
    main()
