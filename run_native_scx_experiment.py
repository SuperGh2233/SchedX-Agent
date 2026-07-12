#!/usr/bin/env python3
"""Compare the default Linux scheduler with SchedX native sched_ext."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
from pathlib import Path

from schedx.benchmark.wrk_parser import parse_wrk_output
from schedx.controllers.scx_controller import (
    SCX_CLASS_BACKGROUND,
    SCX_CLASS_LATENCY,
    ScxController,
)


def process_ids(name: str) -> list[int]:
    result = subprocess.run(
        ["pgrep", "-x", name], capture_output=True, text=True, check=False
    )
    return [int(value) for value in result.stdout.split() if value.isdigit()]


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


def run_phase(duration: int, repeats: int, native: bool) -> dict:
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
        rows = [run_wrk(duration) for _ in range(repeats)]
        stats = ctl.get_stats().to_dict() if native else {}
        return {"rows": rows, "dispatch_stats": stats}
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
    output = Path(os.environ.get("SCHEDX_NATIVE_OUTPUT", "results/native-scx"))
    output.mkdir(parents=True, exist_ok=True)

    default = run_phase(duration, repeats, native=False)
    native = run_phase(duration, repeats, native=True)
    default_rps = mean(default["rows"], "requests_per_sec")
    native_rps = mean(native["rows"], "requests_per_sec")
    default_p99 = mean(default["rows"], "p99_ms")
    native_p99 = mean(native["rows"], "p99_ms")
    result = {
        "kernel": subprocess.check_output(["uname", "-r"], text=True).strip(),
        "duration_seconds": duration,
        "repeats": repeats,
        "default": default,
        "native_sched_ext": native,
        "comparison": {
            "rps_gain_percent": percent_gain(default_rps, native_rps),
            "p99_reduction_percent": percent_drop(default_p99, native_p99),
            "default_mean_rps": default_rps,
            "native_mean_rps": native_rps,
            "default_mean_p99_ms": default_p99,
            "native_mean_p99_ms": native_p99,
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
        f"- Default scheduler mean RPS: {default_rps:.2f}",
        f"- Native sched_ext mean RPS: {native_rps:.2f}",
        f"- RPS gain: {comparison['rps_gain_percent']:.2f}%",
        f"- Default scheduler mean P99: {default_p99:.2f} ms",
        f"- Native sched_ext mean P99: {native_p99:.2f} ms",
        f"- P99 reduction: {comparison['p99_reduction_percent']:.2f}%",
        f"- Native dispatch stats: `{json.dumps(native['dispatch_stats'])}`",
    ]
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(result["comparison"], indent=2))


if __name__ == "__main__":
    main()
