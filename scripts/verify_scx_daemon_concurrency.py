#!/usr/bin/env python3
"""Verify that concurrent Agent tools share one persistent sched_ext daemon."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schedx.scx_daemon import ScxDaemonClient


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--duration", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("results/scx-daemon-concurrency"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    client = ScxDaemonClient()
    before = client.request("status")
    processes = []
    started = time.time()
    intents = ("interactive", "test", "compile", "background")
    for index in range(args.agents):
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "schedx",
                    "tool-run",
                    "--agent-id",
                    f"concurrent-{index}",
                    "--intent",
                    intents[index % len(intents)],
                    "--",
                    "sleep",
                    str(args.duration),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    time.sleep(1)
    active = client.request("policies")
    active_policy_count = len(active["task_policies"]) + len(active["cgroup_policies"])
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate()
        outputs.append({"returncode": process.returncode, "stdout": stdout, "stderr": stderr})
    after = client.request("policies")
    status_after = client.request("status")
    stale_pid = 99999999
    client.set_task_policy(stale_pid, 3, 100)
    time.sleep(6)
    after_reap = client.request("policies")
    status_after_reap = client.request("status")

    records = []
    for path in Path(".schedx/tool-runs").glob("*.json"):
        if path.stat().st_mtime >= started:
            record = json.loads(path.read_text(encoding="utf-8"))
            if str(record.get("agent_id", "")).startswith("concurrent-"):
                records.append(record)

    result = {
        "agents": args.agents,
        "daemon_before": before,
        "active_policy_count": active_policy_count,
        "remaining_policy_count": len(after["task_policies"]) + len(after["cgroup_policies"]),
        "daemon_after": status_after,
        "daemon_after_reap": status_after_reap,
        "successful_tools": sum(item["returncode"] == 0 for item in outputs),
        "native_scx_tools": sum(bool(item.get("native_scx")) for item in records),
        "daemon_mode_tools": sum(item.get("scx_mode") == "daemon" for item in records),
        "stale_policy_reaped": str(stale_pid) not in after_reap["task_policies"],
        "records": records,
        "outputs": outputs,
    }
    result["passed"] = all(
        (
            result["active_policy_count"] >= args.agents,
            result["remaining_policy_count"] == 0,
            result["successful_tools"] == args.agents,
            result["native_scx_tools"] == args.agents,
            result["daemon_mode_tools"] == args.agents,
            status_after["scheduler_running"],
            result["stale_policy_reaped"],
        )
    )
    (args.output / "summary.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    report = [
        "# Persistent sched_ext Daemon Concurrency Verification",
        "",
        f"- Concurrent Agent tools: {args.agents}",
        f"- Active policies observed together: {result['active_policy_count']}",
        f"- Successful tools: {result['successful_tools']}/{args.agents}",
        f"- Native sched_ext tools: {result['native_scx_tools']}/{args.agents}",
        f"- Daemon-mode tools: {result['daemon_mode_tools']}/{args.agents}",
        f"- Policies remaining after completion: {result['remaining_policy_count']}",
        f"- Daemon still running: {status_after['scheduler_running']}",
        f"- Dead PID policy automatically reaped: {result['stale_policy_reaped']}",
        f"- Verification passed: {result['passed']}",
    ]
    (args.output / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key not in {"records", "outputs"}}, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
