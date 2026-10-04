#!/usr/bin/env python3
"""Verify monitor state with real HTTP samples and owned Linux task settings.

The sampling adapter replaces policy selection in this scoped experiment;
it measures monitor behavior and task idempotence, not optimization gains.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schedx.agent.actions import Action
from schedx.agent.context import AgentContext
from schedx.agent.executor import SafeActionExecutor
from schedx.agent.loop import AgentLoop
from schedx.agent.skill import SkillResult
from schedx.controllers.cgroup_controller import CgroupController
from schedx.controllers.process_state import ProcessState
from schedx.policies.verifier import CanaryVerifier
from schedx.state import atomic_json


def task_settings(output: Path) -> dict:
    child = subprocess.Popen(["sleep", "120"])
    owner = "monitor-verification-" + uuid.uuid4().hex
    journal = output / "task-restore.json"
    original = {"nice": os.getpriority(os.PRIO_PROCESS, child.pid),
                "policy": os.sched_getscheduler(child.pid),
                "priority": os.sched_getparam(child.pid).sched_priority,
                "affinity": sorted(os.sched_getaffinity(child.pid))}
    desired = max(original["nice"], 5)
    mask = str(original["affinity"][0])
    actions = [Action("set_nice", str(child.pid), "pid", desired),
               Action("set_sched_policy", str(child.pid), "pid", "SCHED_BATCH"),
               Action("set_affinity", str(child.pid), "pid", mask)]
    try:
        sizes = []
        for index in range(30):
            cgroup = CgroupController(rollback_file=output / "unused-cgroup.json", owner=owner, transaction=str(index), dry_run=False)
            executor = SafeActionExecutor(cgroup)
            executor.process_state = ProcessState(journal, owner, str(index))
            results = executor.execute(actions)
            assert len(results) == 3 and all(row["status"] == "ok" for row in results), results
            if index:
                assert all(row.get("unchanged") for row in results), results
            sizes.append(len(json.loads(journal.read_text())) if journal.exists() else 0)
        assert len(set(sizes)) == 1, sizes
        recovered = ProcessState(journal, owner).rollback()
        assert all(row["status"] == "restored" for row in recovered), recovered
        restored = {"nice": os.getpriority(os.PRIO_PROCESS, child.pid),
                    "policy": os.sched_getscheduler(child.pid),
                    "priority": os.sched_getparam(child.pid).sched_priority,
                    "affinity": sorted(os.sched_getaffinity(child.pid))}
        assert restored == original and not journal.exists(), restored
        return {"status": "passed", "repetitions": 30, "journal_entries": sizes,
                "original_settings_restored": True}
    finally:
        ProcessState(journal, owner).rollback()
        child.terminate()
        child.wait(timeout=5)


def http_monitor(output: Path) -> dict:
    delay = [0.1]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(delay[0])
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    context = AgentContext(state_dir=output / "monitor", data={
        "stability_window": 3, "stability_tolerance_percent": 10,
    })
    loop = AgentLoop(context)
    verifier = CanaryVerifier()
    states = []

    def sample() -> dict:
        values = []
        start = time.monotonic()
        for _ in range(10):
            begin = time.monotonic()
            with urllib.request.urlopen(url, timeout=3) as response:
                assert response.read() == b"ok"
            values.append((time.monotonic() - begin) * 1000)
        return {"p99_ms": sorted(values)[math.ceil(len(values) * 0.99) - 1],
                "requests_per_sec": len(values) / (time.monotonic() - start)}

    def measured_round(index: int) -> dict:
        if index == 4:
            delay[0] = 0.4
        before, after = sample(), sample()
        verdict = verifier.evaluate("latency_first", before, after)
        result = {"round": index, "status": "ok" if verdict.accepted else "canary_rejected",
                  "objective_status": verdict.status,
                  "decision": {"mode": "latency_first", "target": url, "parameters": {}},
                  "metrics": after, "baseline_metrics": before,
                  "improvement": -verdict.deltas["p99_percent"],
                  "configured_http_delay_ms": delay[0] * 1000}
        loop._record_decision_log("verify", SkillResult(verdict.accepted, verdict.status), index)
        print(json.dumps({"round": index, "p99_ms": after["p99_ms"], "verdict": verdict.status}), flush=True)
        return result

    original_save = loop._save_continuous_state

    def save(index: int):
        original_save(index)
        states.append({"round": index, "stable": context.data["converged"],
                       "reason": context.data["stability"]["reason"]})

    loop._run_one_round = measured_round
    loop._save_continuous_state = save
    try:
        reason = loop.run_continuous(interval=0, max_rounds=6)
        saved = json.loads(context.session_file.read_text())["metrics"]["continuous"]
        assert reason == "max_rounds" and saved["completed_round_count"] == 6, saved
        assert states[2]["stable"], states
        assert not states[3]["stable"] and states[3]["reason"] == "objective_variation", states
        assert saved["converged"] and saved["stop_reason"] == "max_rounds", saved
        return {"status": "passed", "source": "real loopback HTTP requests",
                "actuation": "sampling adapter; no service resource changes",
                "stability_window": 3, "stability_tolerance_percent": 10,
                "states": states, "round_history": saved["round_history"]}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("results/continuous-monitor"))
    args = parser.parse_args()
    if os.geteuid() != 0 or not sys.platform.startswith("linux"):
        parser.error("Linux root is required for owned task restoration")
    args.output.mkdir(parents=True, exist_ok=True)
    result = {"kernel": os.uname().release, "cases": {}}
    try:
        result["cases"]["task_settings"] = task_settings(args.output)
        result["cases"]["http_monitor"] = http_monitor(args.output)
        result["status"] = "passed"
    except Exception as exc:
        result.update(status="failed", error=str(exc))
        raise
    finally:
        atomic_json(args.output / "summary.json", result)
    print(json.dumps({"status": result["status"], "output": str(args.output)}), flush=True)


if __name__ == "__main__":
    main()
