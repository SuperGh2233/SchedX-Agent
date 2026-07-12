#!/usr/bin/env python3
"""Compare unmanaged and SchedX-managed Agent tool calls under CPU pressure."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schedx.tool_runner import ToolCallRunner


def cpu_ticks(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            total += int(fields[13]) + int(fields[14])
        except (FileNotFoundError, IndexError, ValueError):
            continue
    return total


def stress_pids() -> list[int]:
    completed = subprocess.run(
        ["pgrep", "-f", "stress-ng.*--cpu"], capture_output=True, text=True, check=False
    )
    return [int(value) for value in completed.stdout.split() if value.isdigit()]


def run_unmanaged(command: list[str]) -> dict:
    started = time.monotonic()
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return {
        "duration_seconds": time.monotonic() - started,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def run_phase(command: list[str], repeats: int, managed: bool, state_dir: Path) -> dict:
    rows = []
    ticks_before = cpu_ticks(stress_pids())
    for repeat in range(1, repeats + 1):
        if managed:
            row = ToolCallRunner(state_dir=state_dir).run(
                command,
                agent_id="benchmark-agent",
                intent="test",
                resource_hint="intent:test,cpu:high,memory:high",
            )
        else:
            row = run_unmanaged(command)
        row["repeat"] = repeat
        rows.append(row)
    ticks_after = cpu_ticks(stress_pids())
    return {"rows": rows, "background_cpu_ticks": ticks_after - ticks_before}


def mean_duration(phase: dict) -> float:
    return statistics.mean(float(row["duration_seconds"]) for row in phase["rows"])


def successful(phase: dict) -> int:
    return sum(int(row["returncode"] == 0) for row in phase["rows"])


def write_report(output: Path, result: dict) -> None:
    comparison = result["comparison"]
    report = [
        "# Agent Tool-Call Resource-Control Experiment",
        "",
        "This experiment compares the same Agent test tool call under full CPU",
        "contention, first unmanaged and then managed by SchedX.",
        "",
        f"- Kernel: `{result['kernel']}`",
        f"- Command: `{' '.join(result['command'])}`",
        f"- Repeats: {result['repeats']}",
        f"- CPU stress workers: {result['stress_cpu']}",
        f"- Unmanaged mean duration: {comparison['unmanaged_mean_seconds']:.4f} s",
        f"- Managed mean duration: {comparison['managed_mean_seconds']:.4f} s",
        f"- Tool-call latency reduction: {comparison['latency_reduction_percent']:.2f}%",
        f"- Unmanaged successful runs: {comparison['unmanaged_successful_runs']}",
        f"- Managed successful runs: {comparison['managed_successful_runs']}",
        f"- Managed native sched_ext runs: {comparison['managed_native_scx_runs']}",
        f"- Background CPU retention: {comparison['background_cpu_retention_percent']:.2f}%",
        "",
        "The managed phase uses an ephemeral hierarchical cgroup, an explicit",
        "Agent resource-intent hint, and the native weighted-vtime sched_ext scheduler.",
    ]
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--stress-cpu", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--output", type=Path, default=Path("results/tool-call-formal"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = [sys.executable, "-m", "pytest", "-q"]

    args.output.mkdir(parents=True, exist_ok=True)
    stress = subprocess.Popen(
        ["stress-ng", "--cpu", str(args.stress_cpu), "--timeout", "30m"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2)
        unmanaged = run_phase(command, args.repeats, False, args.output / "tool-runs")
        managed = run_phase(command, args.repeats, True, args.output / "tool-runs")
    finally:
        stress.terminate()
        try:
            stress.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stress.kill()

    unmanaged_mean = mean_duration(unmanaged)
    managed_mean = mean_duration(managed)
    unmanaged_ticks = unmanaged["background_cpu_ticks"]
    managed_ticks = managed["background_cpu_ticks"]
    comparison = {
        "unmanaged_mean_seconds": unmanaged_mean,
        "managed_mean_seconds": managed_mean,
        "latency_reduction_percent": (
            (unmanaged_mean - managed_mean) / unmanaged_mean * 100 if unmanaged_mean else 0.0
        ),
        "unmanaged_successful_runs": successful(unmanaged),
        "managed_successful_runs": successful(managed),
        "managed_native_scx_runs": sum(int(row.get("native_scx", False)) for row in managed["rows"]),
        "unmanaged_background_cpu_ticks": unmanaged_ticks,
        "managed_background_cpu_ticks": managed_ticks,
        "background_cpu_retention_percent": (
            managed_ticks / unmanaged_ticks * 100 if unmanaged_ticks else 0.0
        ),
    }
    result = {
        "kernel": subprocess.check_output(["uname", "-r"], text=True).strip(),
        "command": command,
        "repeats": args.repeats,
        "stress_cpu": args.stress_cpu,
        "unmanaged": unmanaged,
        "managed": managed,
        "comparison": comparison,
    }
    (args.output / "summary.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    write_report(args.output, result)
    print(json.dumps(comparison, indent=2))


if __name__ == "__main__":
    main()
