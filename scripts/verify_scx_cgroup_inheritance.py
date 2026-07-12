#!/usr/bin/env python3
"""Verify that descendants inherit a cgroup-level sched_ext policy."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schedx.scx_daemon import ScxDaemonClient


def main() -> None:
    client = ScxDaemonClient()
    started = time.time()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "schedx",
            "tool-run",
            "--agent-id",
            "inheritance",
            "--intent",
            "background",
            "--",
            "sh",
            "-c",
            "sleep 4 & wait",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1)
    policies = client.request("policies")
    process.communicate()
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in Path(".schedx/tool-runs").glob("*.json")
        if path.stat().st_mtime >= started
    ]
    record = next(item for item in records if item["agent_id"] == "inheritance")
    cgroup_id = str(record["cgroup_id"])
    result = {
        "cgroup_policy_present": cgroup_id in policies["cgroup_policies"],
        "task_policy_count": len(policies["task_policies"]),
        "policy_scope": record["scx_policy_scope"],
        "native_scx": record["native_scx"],
        "passed": (
            cgroup_id in policies["cgroup_policies"]
            and not policies["task_policies"]
            and record["scx_policy_scope"] == "cgroup"
            and record["native_scx"]
        ),
    }
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
