#!/usr/bin/env python3
"""Real Linux tool bursts: off/fixed/adaptive, full submitted-to-completed times.

This compares admission on the current ToolCallRunner, not an unmodified initial
Agent or native scheduler algorithms. Raw rejected samples are always retained.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import math
import os
import shutil
from pathlib import Path
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schedx.admission import AdmissionConfig, ToolAdmission
from schedx.benchmark.common import describe
from schedx.benchmark.versioning import source_digest
from schedx.controllers.scx_controller import ScxController
from schedx.state import atomic_json
from schedx.tool_runner import ToolCallRunner


def percentile(values: list[float], quantile: float = 0.99) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * quantile) - 1)]


def workload_command(work_cpus: set[int], cpu_seconds: float) -> list[str]:
    code = f"""import os,time
os.sched_setaffinity(0, {sorted(work_cpus)!r})
end = time.process_time() + {cpu_seconds!r}
while time.process_time() < end:
    sum(range(1000))
print('completed')
"""
    return [sys.executable, "-c", code]


def summarize(rows: list[dict], elapsed: float, expected_jobs: int) -> dict:
    failures = []
    if len(rows) != expected_jobs:
        failures.append("missing_tool_results")
    for row in rows:
        if row.get("returncode") != 0 or not row.get("command_started"):
            failures.append(f"unsuccessful_tool:{row.get('index')}")
        if (row.get("native_scx") is not False or row.get("scx_mode") != "cgroup"
                or row.get("cpu_control_support_at_launch", {}).get("cpu_max") != "cgroup_v2"
                or row.get("cpu_control_support", {}).get("cpu_max") != "cgroup_v2"):
            failures.append(f"default_scheduler_not_verified:{row.get('index')}")
        cleanup = row.get("cleanup", {})
        if not all(cleanup.get(key, False) for key in ("policy_removed", "scheduler_stopped", "cgroup_removed")):
            failures.append(f"tool_cleanup_failed:{row.get('index')}")
        if cleanup.get("admission_released") is False:
            failures.append(f"admission_cleanup_failed:{row.get('index')}")
        value = row.get("submitted_to_completed_seconds")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            failures.append(f"invalid_end_to_end_time:{row.get('index')}")
    foreground = [row for row in rows if row.get("intent") == "interactive"]
    background = [row for row in rows if row.get("intent") == "compile"]
    if not foreground or not background:
        failures.append("missing_workload_class")
    cpu_values = [row.get("metrics", {}).get("cpu_stat", {}).get("usage_usec") for row in background]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in cpu_values):
        failures.append("missing_background_cpu_evidence")
    def durations(group):
        return [row["submitted_to_completed_seconds"] for row in group
                if isinstance(row.get("submitted_to_completed_seconds"), (int, float))
                and not isinstance(row["submitted_to_completed_seconds"], bool)
                and math.isfinite(row["submitted_to_completed_seconds"]) and row["submitted_to_completed_seconds"] > 0]
    successful = sum(row.get("returncode") == 0 and bool(row.get("command_started")) for row in rows)
    return {"status": "failed" if failures else "passed", "failures": failures,
            "jobs": len(rows), "successful_jobs": successful,
            "timeouts": sum(bool(row.get("timed_out")) for row in rows), "elapsed_seconds": elapsed,
            "foreground_samples": len(foreground), "background_samples": len(background),
            "tail_estimate_note": "nearest-rank batch P99; small foreground counts approximate the batch maximum, not a service SLO",
            "successful_jobs_per_second": successful / elapsed,
            "foreground_p99_seconds": percentile(durations(foreground)),
            "background_p99_seconds": percentile(durations(background)),
            "background_cpu_seconds_per_wall_second": sum(value for value in cpu_values if isinstance(value, int) and not isinstance(value, bool) and value >= 0) / 1e6 / elapsed}


def assess(rounds: list[dict], minimum_pairs: int = 5) -> dict:
    comparisons = {}
    failures = []
    for mode in ("fixed", "adaptive"):
        metrics = {}
        for metric, direction in (("foreground_p99_seconds", "lower"), ("background_p99_seconds", "lower"),
                                  ("successful_jobs_per_second", "higher")):
            pairs = []
            for repeat in sorted({row["repeat"] for row in rounds}):
                versions = {row["mode"]: row for row in rounds if row["repeat"] == repeat}
                baseline, candidate = versions.get("off"), versions.get(mode)
                invalid = []
                retention = None
                if baseline is None or candidate is None:
                    invalid.append("missing_run")
                else:
                    for name, row in (("off", baseline), (mode, candidate)):
                        invalid.extend(name + ":" + failure for failure in row["failures"])
                    old, new = baseline.get(metric), candidate.get(metric)
                    if any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) or value <= 0 for value in (old, new)):
                        invalid.append("invalid_metric")
                    old_cpu = baseline["background_cpu_seconds_per_wall_second"]
                    if old_cpu > 0:
                        retention = candidate["background_cpu_seconds_per_wall_second"] / old_cpu
                    if retention is None or retention < .25:
                        invalid.append("background_progress_below_25_percent")
                change = None if invalid else 100 * (candidate[metric] - baseline[metric]) / baseline[metric]
                pairs.append({"repeat": repeat, "valid": not invalid, "invalid_reasons": invalid,
                              "background_retention": retention, "change_percent": change})
            valid = [row for row in pairs if row["valid"]]
            statistics = describe(valid, "change_percent")
            interval = statistics["ci95"]
            regression = bool(interval and (interval[0] > 5 if direction == "lower" else interval[1] < -5))
            if len(valid) < minimum_pairs:
                failures.append(f"{mode}:{metric}:insufficient_valid_pairs")
            if regression:
                failures.append(f"{mode}:{metric}:stable_regression_over_5_percent")
            metrics[metric] = {"direction": direction, "pairs": pairs, "statistics": statistics,
                               "valid_pairs": len(valid), "stable_regression_over_5_percent": regression}
        comparisons[mode] = metrics
    return {"status": "not_accepted" if failures else "passed", "failures": failures,
            "comparisons": comparisons, "claim_scope": "current ToolCallRunner with admission off/fixed/adaptive; not initial whole-Agent gains"}


def one_burst(args, root: Path, output: Path, mode: str, repeat: int, work_cpus: set[int]) -> dict:
    output.mkdir()
    config = AdmissionConfig(mode="fixed" if mode == "fixed" else "adaptive", initial_limit=args.limit,
                             max_limit=args.maximum, interactive_reserve=args.reserve)
    runtime = Path("/run/schedx") / ("admission-verify-" + uuid.uuid4().hex)
    # The production CLI defaults to /run. Keep synchronization there too;
    # persistent measurement output must not silently become the runtime queue.
    admission = ToolAdmission(runtime, config, cgroup_root=root) if mode != "off" else None
    runner = ToolCallRunner(root=root, state_dir=output / "tools", native_scx=False, admission=admission)
    stop = threading.Event()
    errors = []
    def monitor():
        try:
            with (output / "control.jsonl").open("w") as log:
                while not stop.wait(.2):
                    snapshot = admission.snapshot()
                    snapshot.pop("jobs", None)
                    log.write(json.dumps({"monotonic": time.monotonic(), **snapshot}) + "\n")
                    log.flush()
        except Exception as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=monitor) if admission else None
    if thread:
        thread.start()
    submitted = time.monotonic()
    def run(index):
        intent = "interactive" if index % 4 == 3 else "compile"
        arrival = submitted + (index // 4) * .08 if intent == "interactive" else submitted
        time.sleep(max(0, arrival - time.monotonic()))
        deadline = time.monotonic() + args.timeout
        cpu_seconds = args.foreground_cpu_seconds if intent == "interactive" else args.cpu_seconds
        start = time.monotonic()
        try:
            row = runner.run(workload_command(work_cpus, cpu_seconds), agent_id="admission-bench-" + str(index % 3),
                             intent=intent, timeout=max(.001, deadline - start), output_limit=4096)
        except Exception as exc:
            row = {"intent": intent, "returncode": 125, "command_started": False, "error": str(exc), "metrics": {}}
        row.update(index=index, submitted_to_completed_seconds=time.monotonic() - start)
        atomic_json(output / f"result-{index:03}.json", row)
        return row
    try:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            rows = list(pool.map(run, range(args.jobs)))
    finally:
        stop.set()
        if thread:
            thread.join(timeout=6)
    summary = summarize(rows, time.monotonic() - submitted, args.jobs)
    if errors:
        summary["failures"].append("control_monitor_failed:" + ";".join(errors))
    if thread and thread.is_alive():
        summary["failures"].append("control_monitor_not_stopped")
    if admission:
        final = admission.snapshot()
        summary["final_admission"] = final
        summary["admission_runtime_directory"] = str(runtime)
        if final["active"] or final["pending"]:
            summary["failures"].append("admission_not_drained")
        shutil.copytree(runtime, output / "admission")
        if not final["active"] and not final["pending"]:
            shutil.rmtree(runtime)
            summary["admission_runtime_removed"] = True
        else:
            summary["admission_runtime_removed"] = False
    summary.update(status="failed" if summary["failures"] else "passed", mode=mode, repeat=repeat,
                   configuration=asdict(config) if admission else None)
    atomic_json(output / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--jobs", type=int, default=24)
    parser.add_argument("--cpu-seconds", type=float, default=1.0)
    parser.add_argument("--foreground-cpu-seconds", type=float, default=.05)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--maximum", type=int, default=8)
    parser.add_argument("--reserve", type=int, default=1)
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("real admission verification requires Linux root; no simulation results are reported")
    if args.repeats < 1 or args.jobs < 4:
        parser.error("at least one repeat and four jobs are required")
    if any(not math.isfinite(value) or value <= 0 for value in (args.cpu_seconds, args.foreground_cpu_seconds, args.timeout)):
        parser.error("work and timeout budgets must be finite and positive")
    AdmissionConfig(initial_limit=args.limit, max_limit=args.maximum, interactive_reserve=args.reserve)
    if ScxController().state() != "disabled":
        parser.error("this cgroup/default-scheduler comparison requires native scheduling already disabled")
    original = set(os.sched_getaffinity(0))
    if len(original) < 2:
        parser.error("one housekeeping CPU and at least one work CPU are required")
    housekeeping = max(original)
    work_cpus = original - {housekeeping}
    parent = Path("/sys/fs/cgroup")
    if not {"cpu", "memory", "pids"} <= set((parent / "cgroup.subtree_control").read_text().split()):
        parser.error("parent controllers must already be enabled; this verifier does not change global configuration")
    args.output.mkdir(parents=True, exist_ok=False)
    root = parent / ("schedx-admission-bench-" + uuid.uuid4().hex)
    report = {"status": "running", "kernel": os.uname().release, "work_cpus": sorted(work_cpus),
              "housekeeping_cpu": housekeeping, "parameters": vars(args) | {"output": str(args.output)},
              "production_source_sha256": source_digest(Path(__file__).resolve().parents[1]),
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "runs": []}
    try:
        root.mkdir()
        os.sched_setaffinity(0, {housekeeping})
        modes = ["off", "fixed", "adaptive"]
        for repeat in range(1, args.repeats + 1):
            for mode in modes[(repeat - 1) % 3:] + modes[:(repeat - 1) % 3]:
                row = one_burst(args, root, args.output / f"repeat-{repeat}-{mode}", mode, repeat, work_cpus)
                report["runs"].append(row)
                atomic_json(args.output / "summary.json", report)
                if row["status"] != "passed":
                    raise RuntimeError("tool execution or cleanup failed; subsequent bursts were not run")
        report["assessment"] = assess(report["runs"])
        report["status"] = report["assessment"]["status"]
        if args.repeats < 5 and not any(row["failures"] for row in report["runs"]):
            report["status"] = "calibration_only"
    except Exception as exc:
        report.update(status="failed", error=str(exc))
    finally:
        os.sched_setaffinity(0, original)
        try:
            root.rmdir()
            report["private_cgroup_removed"] = True
        except OSError as exc:
            report.update(status="failed", private_cgroup_removed=False, cleanup_error=str(exc))
        report["final_scheduler_state"] = ScxController().state()
        if report["final_scheduler_state"] != "disabled":
            report.update(status="failed", backend_error="scheduler state changed during the default-scheduler experiment")
        atomic_json(args.output / "summary.json", report)
    print(json.dumps({"status": report["status"], "output": str(args.output)}))
    return 0 if report["status"] in {"passed", "calibration_only"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
