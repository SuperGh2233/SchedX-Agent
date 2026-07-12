#!/usr/bin/env python3
"""Compare default, rule-based SchedX, and DeepSeek-guided SchedX policies."""

from __future__ import annotations

import json
import argparse
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schedx.agent.decision import Decision
from schedx.benchmark.wrk_parser import parse_wrk_output
from schedx.controllers.scx_controller import (
    SCX_CLASS_BACKGROUND,
    SCX_CLASS_LATENCY,
    ScxController,
)
from schedx.llm.client import LLMError
from schedx.llm.policy_planner import LLMPolicyPlanner


def process_ids(name: str) -> list[int]:
    result = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, check=False)
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


def pressure_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("cpu", "memory", "io"):
        path = Path(f"/proc/pressure/{name}")
        entries = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                kind, *fields = line.split()
                entries[kind] = {
                    key: float(value)
                    for field in fields
                    for key, value in [field.split("=", 1)]
                    if key.startswith("avg")
                }
        except FileNotFoundError:
            pass
        result[name] = entries
    return result


def wait_for_daemon_ready(timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        completed = subprocess.run(
            [sys.executable, "-m", "schedx", "scx-daemon", "status"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode == 0:
            return True
        time.sleep(0.5)
    return False


def classification_snapshot() -> dict[str, Any]:
    return {
        "overall": "mixed",
        "groups": {
            "latency_sensitive": [{"pid": pid, "comm": "nginx"} for pid in process_ids("nginx")],
            "background_noise": [
                {"pid": pid, "comm": "stress-ng"}
                for pid in process_ids("stress-ng") + process_ids("stress-ng-cpu")
            ],
        },
    }


def llm_decision() -> tuple[Decision, str, dict[str, Any], dict[str, Any]]:
    classification = classification_snapshot()
    pressure = pressure_snapshot()
    fallback = Decision(
        mode="latency_first",
        target="nginx",
        parameters={"cpu_weight": 10000, "cpu_weight_bg": 100, "cpu_max_bg": "50000 100000"},
        reason="rule baseline for nginx under CPU interference",
        confidence=0.8,
    )
    try:
        decision = LLMPolicyPlanner().propose(
            classification,
            pressure,
            {"total_cpus": os.cpu_count() or 4},
            fallback,
            ScxController().status(),
        )
        return decision, "deepseek-v4", classification, pressure
    except LLMError as exc:
        fallback.reason = f"LLM fallback: {exc}"
        return fallback, "rule_fallback", classification, pressure


def apply_policy(ctl: ScxController, decision: Decision) -> None:
    latency_weight = int(decision.parameters.get("cpu_weight", 10000))
    background_weight = int(decision.parameters.get("cpu_weight_bg", 100))
    for pid in process_ids("nginx"):
        ctl.set_task_policy(pid, SCX_CLASS_LATENCY, latency_weight)
    for pid in process_ids("stress-ng") + process_ids("stress-ng-cpu"):
        ctl.set_task_policy(pid, SCX_CLASS_BACKGROUND, background_weight)


def run_phase(duration: int, repeats: int, mode: str, decision: Decision | None = None) -> dict:
    ctl = ScxController(dry_run=False)
    policy_source = "none"
    classification: dict[str, Any] = {}
    pressure: dict[str, Any] = {}
    stress = subprocess.Popen(
        ["stress-ng", "--cpu", str(os.cpu_count() or 4), "--timeout", f"{duration * repeats + 20}s"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2)
        if mode != "default":
            if mode == "llm" and decision is None:
                decision, policy_source, classification, pressure = llm_decision()
            elif decision is not None:
                policy_source = "rule"
            ctl.start_scheduler()
            apply_policy(ctl, decision or Decision("latency_first", "nginx", {}, "", 0.8))
        stress_pids = process_ids("stress-ng") + process_ids("stress-ng-cpu")
        ticks_before = cpu_ticks(stress_pids)
        rows = [run_wrk(duration) for _ in range(repeats)]
        ticks_after = cpu_ticks(stress_pids)
        return {
            "rows": rows,
            "background_cpu_ticks": ticks_after - ticks_before,
            "dispatch_stats": ctl.get_stats().to_dict() if mode != "default" else {},
            "policy_source": policy_source,
            "decision": asdict(decision) if decision is not None else {},
            "classification": classification,
            "pressure": pressure,
        }
    finally:
        ctl.stop_scheduler()
        stress.terminate()
        try:
            stress.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stress.kill()
        subprocess.run(["pkill", "-f", "stress-ng"], check=False)


def summarize(default: dict, phase: dict) -> dict[str, float]:
    default_rps = mean(default["rows"], "requests_per_sec")
    phase_rps = mean(phase["rows"], "requests_per_sec")
    default_p99 = mean(default["rows"], "p99_ms")
    phase_p99 = mean(phase["rows"], "p99_ms")
    return {
        "mean_rps": phase_rps,
        "mean_p99_ms": phase_p99,
        "rps_gain_percent": percent_gain(default_rps, phase_rps),
        "p99_reduction_percent": percent_drop(default_p99, phase_p99),
        "background_cpu_retention_percent": (
            phase["background_cpu_ticks"] / default["background_cpu_ticks"] * 100
            if default["background_cpu_ticks"]
            else 0.0
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=int(os.environ.get("SCHEDX_LLM_EXP_DURATION", "10")))
    parser.add_argument("--repeats", type=int, default=int(os.environ.get("SCHEDX_LLM_EXP_REPEATS", "3")))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("SCHEDX_LLM_EXP_OUTPUT", "results/llm-policy-comparison")),
    )
    args = parser.parse_args()
    duration = args.duration
    repeats = args.repeats
    output = args.output
    output.mkdir(parents=True, exist_ok=True)

    daemon_was_active = subprocess.run(
        ["systemctl", "is-active", "--quiet", "schedx-scx-daemon"], check=False
    ).returncode == 0
    if daemon_was_active:
        subprocess.run(["systemctl", "stop", "schedx-scx-daemon"], check=False)
        time.sleep(1)

    rule = Decision(
        "latency_first",
        "nginx",
        {"cpu_weight": 10000, "cpu_weight_bg": 100, "cpu_max_bg": "50000 100000"},
        "deterministic rule policy",
        0.8,
    )
    try:
        default = run_phase(duration, repeats, "default")
        rule_phase = run_phase(duration, repeats, "rule", rule)
        llm_phase = run_phase(duration, repeats, "llm")
    finally:
        if daemon_was_active:
            subprocess.run(["systemctl", "start", "schedx-scx-daemon"], check=False)
            wait_for_daemon_ready()

    llm = llm_phase["decision"]
    llm_source = llm_phase["policy_source"]
    result = {
        "kernel": subprocess.check_output(["uname", "-r"], text=True).strip(),
        "duration_seconds": duration,
        "repeats": repeats,
        "llm_source": llm_source,
        "rule_decision": asdict(rule),
        "llm_decision": llm,
        "default": default,
        "rule_schedx": rule_phase,
        "llm_schedx": llm_phase,
        "comparison": {
            "rule": summarize(default, rule_phase),
            "llm": summarize(default, llm_phase),
        },
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    report = [
        "# DeepSeek Policy Performance Comparison",
        "",
        f"- Kernel: `{result['kernel']}`",
        f"- Duration per repeat: {duration}s",
        f"- Repeats: {repeats}",
        f"- LLM policy source: `{llm_source}`",
        f"- LLM decision: `{json.dumps(llm, ensure_ascii=False)}`",
        f"- LLM classified latency tasks: {len(llm_phase['classification'].get('groups', {}).get('latency_sensitive', []))}",
        f"- LLM classified background tasks: {len(llm_phase['classification'].get('groups', {}).get('background_noise', []))}",
        f"- Rule RPS gain: {result['comparison']['rule']['rps_gain_percent']:.2f}%",
        f"- Rule P99 reduction: {result['comparison']['rule']['p99_reduction_percent']:.2f}%",
        f"- Rule background retention: {result['comparison']['rule']['background_cpu_retention_percent']:.2f}%",
        f"- LLM RPS gain: {result['comparison']['llm']['rps_gain_percent']:.2f}%",
        f"- LLM P99 reduction: {result['comparison']['llm']['p99_reduction_percent']:.2f}%",
        f"- LLM background retention: {result['comparison']['llm']['background_cpu_retention_percent']:.2f}%",
    ]
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(result["comparison"], indent=2))


if __name__ == "__main__":
    main()
