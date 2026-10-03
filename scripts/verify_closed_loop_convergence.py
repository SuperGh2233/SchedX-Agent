#!/usr/bin/env python3
"""Force an unfair starting point and verify runtime-share convergence."""

from __future__ import annotations


import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schedx.controllers.scx_controller import SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL

from schedx.scx_daemon import ScxDaemonClient


def main() -> None:
    output = Path("results/closed-loop-convergence")
    output.mkdir(parents=True, exist_ok=True)
    client = ScxDaemonClient()
    initial_interval = 4096
    client.request("set_target", low=0.30, high=0.40)
    client.request("set_fairness", background_interval=initial_interval, default_interval=SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL)
    intents = ("interactive", "background")
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "schedx",
                "tool-run",
                "--agent-id",
                f"convergence-{index}-{intent}",
                "--intent",
                intent,
                "--",
                "stress-ng",
                "--cpu",
                "4",
                "--timeout",
                "25s",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for index, intent in enumerate(intents)
    ]
    samples = []
    for _ in range(4):
        time.sleep(5.5)
        samples.append(client.request("status"))
    for worker in workers:
        worker.wait()
    client.request("set_target", low=0.12, high=0.25)

    final = samples[-1]["fairness"]
    reasons = [sample["control_telemetry"].get("reason", "") for sample in samples]
    intervals = [sample["fairness"]["background_interval"] for sample in samples]
    result = {
        "initial_interval": initial_interval,
        "sampled_intervals": intervals,
        "sampled_reasons": reasons,
        "final_background_share": final.get("background_runtime_share", 0),
        "passed": min(intervals) < initial_interval and "runtime_share_low" in reasons,
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (output / "report.md").write_text(
        "\n".join(
            [
                "# Closed-Loop Runtime-Share Convergence",
                "",
                f"- Initial background interval: {initial_interval}",
                f"- Sampled intervals: `{intervals}`",
                f"- Sampled reasons: `{reasons}`",
                f"- Final background runtime share: {result['final_background_share']:.2%}",
                f"- Verification passed: {result['passed']}",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
