#!/usr/bin/env python3
"""Verify weighted-vtime fairness between two same-class tasks."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schedx.controllers.scx_controller import SCX_CLASS_BATCH, ScxController


def ticks(pid: int) -> int:
    fields = open(f"/proc/{pid}/stat", encoding="utf-8").read().split()
    return int(fields[13]) + int(fields[14])


def main() -> None:
    low = subprocess.Popen(["taskset", "-c", "0", "yes"], stdout=subprocess.DEVNULL)
    high = subprocess.Popen(["taskset", "-c", "0", "yes"], stdout=subprocess.DEVNULL)
    ctl = ScxController(dry_run=False)
    try:
        ctl.start_scheduler()
        assert ctl.set_task_policy(low.pid, SCX_CLASS_BATCH, 100)
        assert ctl.set_task_policy(high.pid, SCX_CLASS_BATCH, 1000)
        time.sleep(2)
        start_low, start_high = ticks(low.pid), ticks(high.pid)
        time.sleep(10)
        low_ticks = ticks(low.pid) - start_low
        high_ticks = ticks(high.pid) - start_high
        print(
            json.dumps(
                {
                    "low_weight": 100,
                    "high_weight": 1000,
                    "low_ticks": low_ticks,
                    "high_ticks": high_ticks,
                    "observed_ratio": high_ticks / low_ticks if low_ticks else None,
                    "stats": ctl.get_stats().to_dict(),
                },
                indent=2,
            )
        )
    finally:
        ctl.stop_scheduler()
        low.terminate()
        high.terminate()
        low.wait()
        high.wait()
    print(f"state={ctl.state()}")
    print(f"rejected={open('/sys/kernel/sched_ext/nr_rejected', encoding='utf-8').read().strip()}")


if __name__ == "__main__":
    main()
