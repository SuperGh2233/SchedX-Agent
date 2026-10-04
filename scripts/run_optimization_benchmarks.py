#!/usr/bin/env python3
"""Alternate baseline/candidate versions and retain paired performance evidence."""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schedx.benchmark.common import describe
from schedx.benchmark.versioning import source_digest, version_identity
from schedx.state import atomic_json


CASES = {
    "nginx-ablation": ("agent_combined", {"mean_requests_per_sec": "higher", "mean_p99_ms": "lower"}),
    "redis": ("schedx", {"requests_per_sec": "higher", "p99_ms": "lower"}),
    "batch-throughput": ("schedx", {"mean_events_per_second": "higher"}),
}


def require_structured_interface(source: Path) -> None:
    tree = ast.parse((source / "schedx/benchmark/runner.py").read_text())
    supported = next((ast.literal_eval(node.value) for node in ast.walk(tree)
                      if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "SUPPORTED"
                              for target in node.targets)), set())
    missing = set(CASES) - set(supported)
    if missing:
        raise ValueError(f"revision lacks structured benchmark cases {sorted(missing)}; use a common external measurement adapter")


def summarize(rows: list[dict]) -> dict:
    comparisons = {}
    for case, (phase, metrics) in CASES.items():
        pairs = {}
        for row in rows:
            if row["case"] == case:
                pairs.setdefault(row["repeat"], {})[row["version"]] = row["summary"]["phases"][phase]
        results = {}
        for metric, direction in metrics.items():
            samples = []
            for repeat, pair in pairs.items():
                if set(pair) != {"baseline", "candidate"}:
                    continue
                before, after = pair["baseline"].get(metric), pair["candidate"].get(metric)
                if isinstance(before, dict):
                    before = before.get("mean")
                if isinstance(after, dict):
                    after = after.get("mean")
                if before is None or after is None or before <= 0:
                    continue
                samples.append({"repeat": repeat, "baseline": before, "candidate": after, "change_percent": (after / before - 1) * 100,
                                "fairness_valid": bool((pair["baseline"].get("background_retention_percent") or 0) >= 25 and (pair["candidate"].get("background_retention_percent") or 0) >= 25)})
            statistics = describe(samples, "change_percent")
            confidence = statistics["ci95"]
            regressed = bool(confidence and (confidence[1] < -5 if direction == "higher" else confidence[0] > 5))
            results[metric] = {"direction": direction, "samples": samples, "statistics": statistics,
                               "stable_regression_over_5_percent": regressed,
                               "all_samples_fairness_valid": bool(samples) and all(row["fairness_valid"] for row in samples)}
        comparisons[case] = results
    return comparisons


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline-bin", type=Path, default=Path("/usr/local/bin"))
    parser.add_argument("--candidate-bin", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("results/optimization-performance"))
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--redis-port", type=int, default=6380)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--candidate-ref", required=True)
    parser.add_argument("--baseline-build-manifest", type=Path, required=True)
    parser.add_argument("--candidate-build-manifest", type=Path, required=True)
    args = parser.parse_args()
    identities = {
        version: version_identity(
            args.repository, getattr(args, version).resolve(), getattr(args, version + "_ref"),
            getattr(args, version + "_bin") / "scx_agent", getattr(args, version + "_build_manifest"),
        ) for version in ("baseline", "candidate")
    }
    # This legacy runner requires the structured benchmark API in both trees.
    # Preliminary July sources require the common external measurement adapter.
    for version in identities:
        try:
            require_structured_interface(getattr(args, version))
        except ValueError as exc:
            parser.error(f"{version}: {exc}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if Path("/sys/kernel/sched_ext/state").read_text().strip() != "disabled":
        raise RuntimeError("performance comparison requires no active native scheduler")
    report = {"baseline_commit": identities["baseline"]["commit"], "candidate_commit": identities["candidate"]["commit"],
              "version_identity": identities, "kernel": os.uname().release, "repeats": args.repeats, "duration": args.duration,
              "source_sha256": {version: source_digest(path.resolve()) for version, path in (("baseline", args.baseline), ("candidate", args.candidate))}, "runs": []}
    redis_log = (output / "redis-server.log").open("w")
    redis = subprocess.Popen(["redis-server", "--bind", "127.0.0.1", "--port", str(args.redis_port), "--save", "", "--appendonly", "no", "--dir", str(output)], stdout=redis_log, stderr=redis_log)
    try:
        time.sleep(1)
        if redis.poll() is not None:
            raise RuntimeError("isolated Redis server failed to start")
        for repeat in range(1, args.repeats + 1):
            versions = ("baseline", "candidate") if repeat % 2 else ("candidate", "baseline")
            for case in CASES:
                for version in versions:
                    source = getattr(args, version).resolve()
                    env = dict(os.environ)
                    env["PATH"] = str(getattr(args, version + "_bin").resolve()) + ":" + env["PATH"]
                    destination = output / case / f"repeat-{repeat}" / version
                    destination.mkdir(parents=True)
                    command = [sys.executable, "-m", "schedx", "benchmark", case, "--duration", str(args.duration), "--repeats", "1", "--warmup", "1", "--redis-port", str(args.redis_port), "--output", str(destination)]
                    completed = subprocess.run(command, cwd=source, env=env, capture_output=True, text=True, timeout=300)
                    (destination / "stdout.json").write_text(completed.stdout)
                    (destination / "stderr.log").write_text(completed.stderr)
                    if completed.returncode:
                        raise RuntimeError(f"{case}/{version} failed: {completed.stderr[-2000:]}")
                    result = json.loads(completed.stdout)
                    if result.get("status") != "ok":
                        raise RuntimeError(f"{case}/{version}: {result}")
                    report["runs"].append({"repeat": repeat, "case": case, "version": version, "run_dir": result["run_dir"], "summary": result["summary"]})
                    report["comparisons"] = summarize(report["runs"])
                    atomic_json(output / "summary.json", report)
                    print(json.dumps({"repeat": repeat, "case": case, "version": version, "status": "complete"}), flush=True)
        report["status"] = "complete"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        redis.terminate()
        redis.wait(timeout=10)
        redis_log.close()
        report["final_sched_ext_state"] = Path("/sys/kernel/sched_ext/state").read_text().strip()
        atomic_json(output / "summary.json", report)


if __name__ == "__main__":
    main()
