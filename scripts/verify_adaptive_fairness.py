#!/usr/bin/env python3
"""Verify PSI-driven fairness control with competing workload classes."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schedx.scx_daemon import ScxDaemonClient


def main() -> None:
    output = Path("results/adaptive-fairness")
    output.mkdir(parents=True, exist_ok=True)
    client = ScxDaemonClient()
    stats_before = client.request("stats")["stats"]
    workers = []
    for agent, intent in (("latency-agent", "interactive"), ("background-agent", "background")):
        workers.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "schedx",
                    "tool-run",
                    "--agent-id",
                    agent,
                    "--intent",
                    intent,
                    "--",
                    "stress-ng",
                    "--cpu",
                    "4",
                    "--timeout",
                    "20s",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    time.sleep(12)
    active = client.request("policies")
    status_during = client.request("status")
    stats_during = client.request("stats")["stats"]
    cgroup_metrics = client.request("cgroup_metrics")["cgroup_metrics"]
    outputs = []
    for worker in workers:
        stdout, stderr = worker.communicate()
        outputs.append({"returncode": worker.returncode, "stdout": stdout, "stderr": stderr})
    status_after = client.request("status")

    result = {
        "fairness_during": status_during["fairness"],
        "active_cgroup_policies": len(active["cgroup_policies"]),
        "cgroup_metrics": cgroup_metrics,
        "control_telemetry": status_during["control_telemetry"],
        "latency_dispatch_delta": stats_during["latency_dispatches"]
        - stats_before["latency_dispatches"],
        "background_dispatch_delta": stats_during["background_dispatches"]
        - stats_before["background_dispatches"],
        "successful_workloads": sum(item["returncode"] == 0 for item in outputs),
        "daemon_running_after": status_after["scheduler_running"],
    }
    result["passed"] = all(
        (
            result["active_cgroup_policies"] == 2,
            len(result["cgroup_metrics"]) == 2,
            all(metric["runtime_ns"] > 0 for metric in result["cgroup_metrics"].values()),
            result["latency_dispatch_delta"] > 0,
            result["background_dispatch_delta"] > 0,
            result["successful_workloads"] == 2,
            result["daemon_running_after"],
        )
    )
    (output / "summary.json").write_text(
        json.dumps({**result, "outputs": outputs}, indent=2), encoding="utf-8"
    )
    report = [
        "# Adaptive Fairness Verification",
        "",
        f"- Fairness mode under contention: `{result['fairness_during']['mode']}`",
        f"- CPU PSI avg10: {result['fairness_during'].get('cpu_pressure_avg10', 0)}",
        f"- Background service interval: {result['fairness_during']['background_interval']}",
        f"- Default service interval: {result['fairness_during']['default_interval']}",
        f"- Active cgroup policies: {result['active_cgroup_policies']}",
        f"- Cgroups with scheduler metrics: {len(result['cgroup_metrics'])}",
        f"- Background runtime share: {result['control_telemetry'].get('background_share', 0):.2%}",
        f"- Closed-loop reason: `{result['control_telemetry'].get('reason', '')}`",
        f"- Latency dispatch delta: {result['latency_dispatch_delta']}",
        f"- Background dispatch delta: {result['background_dispatch_delta']}",
        f"- Verification passed: {result['passed']}",
    ]
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
