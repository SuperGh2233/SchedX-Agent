#!/usr/bin/env python3
"""Run concurrent Agent tool calls and capture a live DeepSeek policy plan."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schedx.scx_daemon import ScxDaemonClient


def run_json(command: list[str], timeout: int = 20) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        data = {}
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "json": data,
    }


def tool_command(agent_id: str, intent: str, duration: int) -> list[str]:
    if intent == "interactive":
        payload = ["sleep", str(duration)]
    else:
        payload = ["stress-ng", "--cpu", "1", "--timeout", f"{duration}s"]
    return [
        sys.executable,
        "-m",
        "schedx",
        "tool-run",
        "--agent-id",
        agent_id,
        "--intent",
        intent,
        "--",
        *payload,
    ]


def recent_tool_records(started: float, agent_prefix: str) -> list[dict[str, Any]]:
    records = []
    state_dir = Path(".schedx/tool-runs")
    if not state_dir.exists():
        return records
    for path in sorted(state_dir.glob("*.json")):
        if path.stat().st_mtime < started:
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if str(record.get("agent_id", "")).startswith(agent_prefix):
            records.append(record)
    return records


def write_report(output: Path, result: dict[str, Any]) -> None:
    llm_decision = result.get("llm_plan", {}).get("json", {}).get("decision", {})
    report = [
        "# Multi-Agent LLM Scheduling Experiment",
        "",
        "This experiment runs concurrent Agent tool calls through the persistent",
        "sched_ext daemon and captures a live DeepSeek policy plan while those",
        "tools are active.",
        "",
        f"- Agents: {result['agents']}",
        f"- Duration: {result['duration_seconds']}s",
        f"- Active policies observed: {result['active_policy_count']}",
        f"- Successful tools: {result['successful_tools']}/{result['agents']}",
        f"- Native sched_ext tools: {result['native_scx_tools']}/{result['agents']}",
        f"- Daemon-mode tools: {result['daemon_mode_tools']}/{result['agents']}",
        f"- Policies remaining after completion: {result['remaining_policy_count']}",
        f"- Daemon running after experiment: {result['daemon_after'].get('scheduler_running')}",
        f"- LLM source: `{llm_decision.get('source', result['llm_plan'].get('json', {}).get('llm_source', 'unknown'))}`",
        f"- LLM mode: `{llm_decision.get('mode', 'n/a')}`",
        f"- LLM target: `{llm_decision.get('target', 'n/a')}`",
        f"- Verification passed: {result['passed']}",
    ]
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", type=int, default=6)
    parser.add_argument("--duration", type=int, default=8)
    parser.add_argument("--output", type=Path, default=Path("results/multi-agent-llm"))
    parser.add_argument("--agent-prefix", default="llm-agent-")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    client = ScxDaemonClient()
    daemon_before = client.request("status")
    started = time.time()
    intents = ["interactive", "test", "compile", "background", "test", "background"]
    processes = []
    for index in range(args.agents):
        intent = intents[index % len(intents)]
        agent_id = f"{args.agent_prefix}{index}"
        processes.append(
            subprocess.Popen(
                tool_command(agent_id, intent, args.duration),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    time.sleep(1.5)
    active = client.request("policies")
    metrics_during = client.request("cgroup_metrics")
    llm_plan = run_json([sys.executable, "-m", "schedx", "llm-plan"], timeout=45)

    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=args.duration + 20)
        outputs.append({"returncode": process.returncode, "stdout": stdout, "stderr": stderr})

    after = client.request("policies")
    daemon_after = client.request("status")
    cleanup = client.request("cleanup_metrics")
    records = recent_tool_records(started, args.agent_prefix)

    active_policy_count = len(active.get("task_policies", {})) + len(active.get("cgroup_policies", {}))
    remaining_policy_count = len(after.get("task_policies", {})) + len(after.get("cgroup_policies", {}))
    result = {
        "agents": args.agents,
        "duration_seconds": args.duration,
        "daemon_before": daemon_before,
        "daemon_after": daemon_after,
        "active_policy_count": active_policy_count,
        "remaining_policy_count": remaining_policy_count,
        "metrics_during": metrics_during,
        "cleanup": cleanup,
        "llm_plan": llm_plan,
        "successful_tools": sum(item["returncode"] == 0 for item in outputs),
        "native_scx_tools": sum(bool(item.get("native_scx")) for item in records),
        "daemon_mode_tools": sum(item.get("scx_mode") == "daemon" for item in records),
        "records": records,
        "outputs": outputs,
    }
    result["passed"] = all(
        (
            daemon_before.get("scheduler_running"),
            daemon_after.get("scheduler_running"),
            active_policy_count >= args.agents,
            remaining_policy_count == 0,
            result["successful_tools"] == args.agents,
            result["native_scx_tools"] == args.agents,
            result["daemon_mode_tools"] == args.agents,
            llm_plan["returncode"] == 0,
            bool(llm_plan["json"].get("decision")),
        )
    )
    (args.output / "summary.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    write_report(args.output, result)
    print(
        json.dumps(
            {key: value for key, value in result.items() if key not in {"records", "outputs"}},
            indent=2,
            ensure_ascii=False,
        )
    )
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
