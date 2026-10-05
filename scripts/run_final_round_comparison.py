#!/usr/bin/env python3
"""Common external measurements for preliminary/final native scheduler binaries.

Both binaries use identical, explicitly recorded policy commands. The default
profile preserves compiled fairness defaults. An optional shared throughput
profile uses the current Agent's existing batch settings on both binaries.
This measures the native component, not the entire autonomous Agent.
Unsupported historical Python benchmark APIs are never invoked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schedx.benchmark.common import describe
from schedx.benchmark.redis_parser import parse_redis_benchmark_output
from schedx.benchmark.scoped_workloads import OwnedWorkloads, process_ticks
from schedx.benchmark.sysbench_parser import parse_sysbench_output
from schedx.benchmark.versioning import version_identity
from schedx.benchmark.wrk_parser import parse_wrk_output
from schedx.controllers.scx_controller import (
    ScxController, SCX_FAIRNESS_THROUGHPUT_BACKGROUND, SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL,
    SCX_WEIGHT_DEFAULTS, SCX_CLASS_LATENCY, SCX_CLASS_BACKGROUND, SCX_CLASS_BATCH,
)
from schedx.policies.scx_mapper import ScxPolicyMapper
from schedx.cpu_backend import CpuBackendLease, backend_lock_path
from schedx.policies.verifier import CanaryVerifier
from schedx.scx_daemon import ScxDaemonClient, _runtime_shares, choose_background_interval
from schedx.state import atomic_json


METRICS = {"nginx": {"requests_per_sec": "higher", "p99_ms": "lower"},
           "redis": {"requests_per_sec": "higher", "p99_ms": "lower"},
           "batch": {"events_per_second": "higher"}}
POLICIES = {"service": (1, 10000), "redis": (1, 10000), "noise": (3, 100), "batch": (2, 1500)}


def profile_settings(profile: str, case: str) -> dict:
    if profile not in {"compiled-defaults", "shared-throughput", "shared-adaptive"}:
        raise ValueError("unsupported native comparison profile")
    if profile == "compiled-defaults" or case != "batch":
        return {"policies": POLICIES, "fairness": None,
                "description": "common static policies; each binary retains compiled fairness defaults"}
    mapper = ScxPolicyMapper(ScxController(dry_run=True))
    # Existing production parameters, not values searched against this dataset.
    policies = {
        role: (class_id, mapper._adjust_weight(SCX_WEIGHT_DEFAULTS[class_id], group, "throughput_first"))
        for role, class_id, group in (
            ("service", SCX_CLASS_LATENCY, "latency_sensitive"),
            ("redis", SCX_CLASS_LATENCY, "latency_sensitive"),
            ("noise", SCX_CLASS_BACKGROUND, "background_noise"),
            ("batch", SCX_CLASS_BATCH, "batch_compute"))
    }
    return {"policies": policies,
            "fairness": [SCX_FAIRNESS_THROUGHPUT_BACKGROUND, SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL],
            "adaptive": profile == "shared-adaptive", "target_runtime_share": [0.15, 0.25],
            "description": "same existing Agent throughput settings on both binaries; batch only; feedback choice recorded explicitly"}


def execution_order(repeat: int) -> list[str]:
    order = ["baseline", "candidate"] if repeat % 2 else ["candidate", "baseline"]
    order.insert((repeat - 1) % 3, "reference")
    return order


class NativeSession:
    """Bounded IPC without changing either binary's compiled fairness defaults."""

    def __init__(self, binary: Path, groups: dict[str, Path], output: Path,
                 *, profile: str = "compiled-defaults", case: str = "nginx"):
        self.binary, self.groups, self.output = binary, groups, output
        self.controller = ScxController(dry_run=False)
        self.threads = []
        self.logs = []
        self.lease = None
        self.settings = profile_settings(profile, case)
        self.feedback_stop = threading.Event()
        self.feedback_thread = None
        self.feedback_errors = []
        self.feedback_samples = []
        self.evidence = {"binary": str(binary), "fairness_configuration": "compiled defaults; no set fairness command"}
        self.evidence.update(profile=profile, settings=self.settings)

    def __enter__(self):
        if self.controller.state() != "disabled":
            raise RuntimeError("another native scheduler is active")
        self.lease = CpuBackendLease(backend_lock_path(self.controller.sys_root), native=True).acquire()
        self.controller._process = subprocess.Popen([str(self.binary.resolve())], stdin=subprocess.PIPE,
                                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                                                    pass_fds=(self.lease.fd,))
        for name in ("stdout", "stderr"):
            log = (self.output / f"native-{name}.log").open("w")
            self.logs.append(log)
            stream = getattr(self.controller._process, name)
            thread = threading.Thread(target=self._pump, args=(stream, log, name), daemon=True)
            thread.start()
            self.threads.append(thread)
        try:
            self.evidence["startup"] = self.controller._read_until_prompt()
            if self.controller.state() != "enabled" or self.controller.current_scheduler() != "schedx_agent":
                raise RuntimeError("requested native binary did not attach")
            self.evidence["policies"] = {}
            if self.settings["fairness"] is not None:
                acknowledged = self.controller.set_fairness(*self.settings["fairness"])
                self.evidence["fairness_configuration"] = self.settings["description"]
                self.evidence["fairness_acknowledged"] = acknowledged
                if not acknowledged:
                    raise RuntimeError("shared fairness configuration was not acknowledged")
            for role, group in self.groups.items():
                class_id, weight = self.settings["policies"][role]
                inode = group.stat().st_ino
                accepted = self.controller.set_cgroup_policy(inode, class_id, weight)
                self.evidence["policies"][role] = {"cgroup_id": inode, "class_id": class_id,
                                                   "weight": weight, "acknowledged": accepted}
                if not accepted:
                    raise RuntimeError(f"native policy was not acknowledged: {role}")
            if self.settings.get("adaptive"):
                self.feedback_thread = threading.Thread(target=self._adapt_cgroup_runtime, daemon=True)
                self.feedback_thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def _pump(self, stream, log, name):
        for line in iter(stream.readline, ""):
            log.write(line)
            log.flush()
            if name == "stdout":
                self.controller._stdout_lines.put(line)
            else:
                self.controller._stderr_tail.append(line)
        if name == "stdout":
            self.controller._stdout_lines.put(None)

    def alive(self) -> bool:
        return self.controller._process is not None and self.controller._process.poll() is None and self.controller.state() == "enabled"

    def _adapt_cgroup_runtime(self):
        # The historical binary has cgroup runtime metrics, but lacks the newer
        # class-metrics command. Use the same production feedback rule and
        # target on both; this is a declared common measurement adapter.
        policies = {group.stat().st_ino: {"class_id": self.settings["policies"][role][0]}
                    for role, group in self.groups.items()}
        previous = None
        while not self.feedback_stop.wait(1.0):
            try:
                current = self.controller.get_cgroup_metrics()
                shares = _runtime_shares(policies, previous or {}, current)
                total, background = shares["sample_runtime_ns"], shares["background_runtime_ns"]
                if previous is not None and total > background:
                    before = self.controller.background_interval
                    reason, interval = choose_background_interval(
                        before, shares["background_share"], total, 0, True, 0.15, 0.25)
                    if interval != before and not self.controller.set_fairness(interval, SCX_FAIRNESS_DEFAULT_CLASS_INTERVAL):
                        raise RuntimeError("adaptive fairness command was not acknowledged")
                    self.feedback_samples.append({"background_runtime_share": shares["background_share"],
                                                  "sample_runtime_ns": total, "previous_interval": before,
                                                  "interval": interval, "reason": reason})
                previous = current
            except (OSError, RuntimeError, ValueError) as exc:
                self.feedback_errors.append(f"{type(exc).__name__}: {exc}")
                return

    def close(self):
        self.feedback_stop.set()
        if self.feedback_thread:
            self.feedback_thread.join(timeout=self.controller.command_timeout + 2)
        self.evidence["feedback"] = {"samples": self.feedback_samples, "errors": self.feedback_errors,
                                     "stopped": not self.feedback_thread or not self.feedback_thread.is_alive()}
        stopped = self.controller.stop_scheduler()
        self.evidence["scheduler_stopped"] = stopped
        if stopped and self.lease is not None:
            self.lease.release()
            self.lease = None
        for thread in self.threads:
            thread.join(timeout=2)
        self.evidence["output_readers_stopped"] = not any(thread.is_alive() for thread in self.threads)
        if self.evidence["output_readers_stopped"]:
            for log in self.logs:
                log.close()
        self.evidence["final_scheduler_state"] = self.controller.state()


def client_metrics(case: str, raw: str, returncode: int) -> tuple[dict, list[str]]:
    failures = []
    if returncode != 0:
        failures.append("client_nonzero_exit")
    if case == "nginx":
        metrics = parse_wrk_output(raw)
        verdict = CanaryVerifier().evaluate("balanced", metrics, metrics, error_metrics_expected=True)
        if not verdict.accepted or verdict.status != "accepted":
            failures.extend(verdict.reasons)
        if "successful_requests_per_sec" in metrics:
            metrics["raw_requests_per_sec"] = metrics.get("requests_per_sec")
            metrics["requests_per_sec"] = metrics["successful_requests_per_sec"]
    elif case == "redis":
        metrics = parse_redis_benchmark_output(raw)
        if re.search(r"(?:^|\n)\s*(?:Error:|Error from server:|Unexpected error reply)", raw, re.IGNORECASE):
            failures.append("redis_client_error")
        if not metrics.get("requests") or not metrics.get("elapsed_seconds"):
            failures.append("missing_redis_completion")
    else:
        metrics = parse_sysbench_output(raw)
        if not metrics.get("events") or not metrics.get("elapsed_seconds"):
            failures.append("missing_batch_completion")
    for metric in METRICS[case]:
        value = metrics.get(metric)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            failures.append(f"missing_or_invalid_{metric}")
    return metrics, failures


def compare(rows: list[dict], minimum_pairs: int = 5) -> dict:
    results, failures = {}, []
    for case, metrics in METRICS.items():
        rounds = {}
        for row in rows:
            if row["case"] == case:
                rounds.setdefault(row["repeat"], {})[row["version"]] = row
        results[case] = {}
        for metric, direction in metrics.items():
            pairs = []
            for repeat, versions in sorted(rounds.items()):
                invalid = []
                retention = {}
                if set(versions) != {"reference", "baseline", "candidate"}:
                    invalid.append("missing_version_or_reference")
                reference = versions.get("reference", {}).get("background_cpu_seconds_per_wall_second")
                for version in ("reference", "baseline", "candidate"):
                    invalid.extend(f"{version}:{reason}" for reason in versions.get(version, {}).get("failures", []))
                for version in ("baseline", "candidate"):
                    progress = versions.get(version, {}).get("background_cpu_seconds_per_wall_second")
                    if any(isinstance(value, bool) or not isinstance(value, (float, int))
                           or not math.isfinite(value) or value < 0 for value in (reference, progress)) or reference == 0:
                        invalid.append("missing_background_reference")
                    else:
                        retention[version] = progress / reference
                        if retention[version] < 0.25:
                            invalid.append(f"{version}:background_retention_below_25_percent")
                before = versions.get("baseline", {}).get("metrics", {}).get(metric)
                after = versions.get("candidate", {}).get("metrics", {}).get(metric)
                values = (before, after)
                if any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or value < 0 for value in values) or before == 0:
                    invalid.append("metric_not_comparable")
                delta = (after / before - 1) * 100 if not invalid else None
                if delta is not None and not math.isfinite(delta):
                    invalid.append("nonfinite_delta")
                    delta = None
                pairs.append({"repeat": repeat, "baseline": before, "candidate": after,
                              "background_retention": retention, "change_percent": delta,
                              "valid": not invalid, "invalid_reasons": sorted(set(invalid))})
            valid = [pair for pair in pairs if pair["valid"]]
            statistics = describe(valid, "change_percent")
            interval = statistics["ci95"]
            regressed = bool(interval and (interval[1] < -5 if direction == "higher" else interval[0] > 5))
            if len(valid) < minimum_pairs:
                failures.append(f"{case}:{metric}:insufficient_valid_pairs")
            if regressed:
                failures.append(f"{case}:{metric}:stable_regression_over_5_percent")
            results[case][metric] = {"direction": direction, "pairs": pairs,
                                     "valid_pair_count": len(valid), "statistics": statistics,
                                     "stable_regression_over_5_percent": regressed}
    return {"status": "passed" if not failures else "not_accepted", "failures": failures, "comparisons": results}


def measure(case, version, binary, workloads, output, duration, warmup, repeat, threads,
            *, profile="compiled-defaults"):
    output.mkdir(parents=True, exist_ok=False)
    row = {"case": case, "version": version, "repeat": repeat, "failures": [],
           "profile": profile, "settings": profile_settings(profile, case)}
    native = None
    try:
        workloads.noise(True)
        if version != "reference":
            native = NativeSession(binary, workloads.groups, output, profile=profile, case=case)
            native.__enter__()
        time.sleep(warmup)
        background_before = workloads.pids("noise")
        before = process_ticks(background_before)
        start = time.monotonic()
        if case == "nginx":
            command = ["wrk", "-t2", "-c64", f"-d{duration}s", "--latency", workloads.url]
        elif case == "redis":
            command = ["redis-benchmark", "-h", "127.0.0.1", "-p", str(workloads.redis_port),
                       "-n", str(duration * 50000), "-c", "64", "--threads", "2", "--precision", "3", "-t", "get"]
        else:
            command = ["sysbench", "cpu", f"--threads={threads}", f"--time={duration}", "run"]
        if case == "batch":
            process = workloads.spawn("batch", command)
            process.wait(timeout=duration + 30)
            raw = Path(workloads.logs[-1].name).read_text()
            returncode = process.returncode
        else:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=duration * 5 + 30)
            raw = completed.stdout + completed.stderr
            returncode = completed.returncode
        elapsed = time.monotonic() - start
        background_after = workloads.pids("noise")
        after = process_ticks(background_after)
        if not background_before or background_before != background_after:
            row["failures"].append("background_process_set_changed")
        (output / "client-output.txt").write_text(raw)
        row.update(command=command, client_returncode=returncode, wall_seconds=elapsed,
                   background_cpu_ticks=max(0, after - before),
                   background_cpu_seconds_per_wall_second=max(0, after - before) / os.sysconf("SC_CLK_TCK") / elapsed)
        row["metrics"], client_failures = client_metrics(case, raw, returncode)
        row["failures"].extend(client_failures)
        if case == "redis" and row["metrics"].get("requests") != duration * 50000:
            row["failures"].append("redis_incomplete_work")
        if native is not None and not native.alive():
            row["failures"].append("unexpected_native_scheduler_exit")
        if version == "reference" and ScxController().state() != "disabled":
            row["failures"].append("reference_scheduler_changed")
    except Exception as exc:
        row["failures"].append(f"{type(exc).__name__}: {exc}")
        if isinstance(exc, subprocess.TimeoutExpired):
            pieces = [value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
                      for value in (exc.stdout, exc.stderr)]
            (output / "client-output-partial.txt").write_text("".join(pieces))
    except BaseException as exc:
        row["failures"].append(f"{type(exc).__name__}: interrupted")
        raise
    finally:
        if native is not None:
            native.close()
            row["native_evidence"] = native.evidence
            if not native.evidence.get("output_readers_stopped"):
                row["failures"].append("native_output_reader_still_running")
            if not native.evidence.get("scheduler_stopped"):
                row["failures"].append("native_scheduler_stop_failed")
            feedback = native.evidence["feedback"]
            if not feedback["stopped"] or feedback["errors"]:
                row["failures"].append("native_feedback_failed_or_not_stopped")
            if native.settings.get("adaptive") and not feedback["samples"]:
                row["failures"].append("missing_adaptive_runtime_evidence")
        workloads.stop("batch")
        workloads.stop("noise")
        row["final_scheduler_state"] = ScxController().state()
        if row["final_scheduler_state"] != "disabled":
            row["failures"].append("native_cleanup_incomplete")
        atomic_json(output / "measurement.json", row)
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    for version in ("baseline", "candidate"):
        parser.add_argument(f"--{version}-source", type=Path, required=True)
        parser.add_argument(f"--{version}-ref", required=True)
        parser.add_argument(f"--{version}-binary", type=Path, required=True)
        parser.add_argument(f"--{version}-build-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--warmup", type=float, default=1.0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--batch-threads", type=int, default=3)
    parser.add_argument("--profile", choices=("compiled-defaults", "shared-throughput", "shared-adaptive"),
                        default="compiled-defaults", help="explicit common batch configuration; never patches either algorithm")
    args = parser.parse_args()
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        parser.error("native comparison requires Linux root")
    if args.repeats < 5 or args.duration < 1 or args.workers < 1 or args.batch_threads < 1 or not math.isfinite(args.warmup) or args.warmup < 0:
        parser.error("at least five pairs, positive workloads and finite non-negative warmup are required")
    if ScxController().state() != "disabled" or ScxDaemonClient().is_available():
        parser.error("comparison requires no active native scheduler or persistent daemon")
    if shutil.which("sysbench") is None:
        parser.error("sysbench is required")
    identities = {version: version_identity(args.repository, getattr(args, version + "_source"),
                                            getattr(args, version + "_ref"), getattr(args, version + "_binary"),
                                            getattr(args, version + "_build_manifest"))
                  for version in ("baseline", "candidate")}
    btf_digest = hashlib.sha256(Path("/sys/kernel/btf/vmlinux").read_bytes()).hexdigest()
    for version, identity in identities.items():
        manifest = identity["build_manifest"]
        if manifest.get("kernel_release") != os.uname().release or manifest.get("btf_sha256") != btf_digest:
            parser.error(f"{version} build manifest does not match this kernel/BTF")
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 2:
        parser.error("two or more allowed CPUs are needed to reserve a measurement/control CPU")
    work_cpus, housekeeping_cpu = set(allowed[:-1]), allowed[-1]
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"status": "running", "comparison_scope": "native sched_ext component with common static class policies",
              "profile": args.profile,
              "fairness_parameters": ("each binary's compiled defaults; preliminary algorithm and settings are not patched"
                                      if args.profile == "compiled-defaults" else
                                      "existing Agent throughput settings applied equally for batch; shared-adaptive uses the same cgroup runtime feedback on both; service cases retain compiled defaults"),
              "settings_by_case": {case: profile_settings(args.profile, case) for case in METRICS},
              "version_identity": identities, "kernel": os.uname().release,
              "work_cpus": sorted(work_cpus), "housekeeping_cpu": housekeeping_cpu,
              "background_progress_definition": "owned noise CPU seconds / wall second; minimum 25% of paired default-scheduler reference",
              "duration": args.duration, "repeats": args.repeats, "runs": [], "execution_order": []}
    workloads = OwnedWorkloads(args.output / "workloads", workers=args.workers, redis=True, cpus=work_cpus)
    try:
        os.sched_setaffinity(0, {housekeeping_cpu})
        workloads.__enter__()
        # Check the installed client's error behavior on an owned Redis server.
        error_probe = subprocess.run(["redis-benchmark", "-h", "127.0.0.1", "-p", str(workloads.redis_port),
                                      "-n", "1", "-c", "1", "SCHEDX_UNKNOWN_COMMAND"],
                                     capture_output=True, text=True, timeout=5)
        error_text = error_probe.stdout + error_probe.stderr
        (args.output / "redis-error-probe.txt").write_text(error_text)
        if error_probe.returncode == 0 or "unknown command" not in error_text.lower():
            raise RuntimeError("installed Redis benchmark did not demonstrate fatal server-error reporting")
        report["redis_error_probe"] = {"returncode": error_probe.returncode, "fatal_server_error_observed": True}
        for repeat in range(1, args.repeats + 1):
            cases = list(METRICS)
            offset = (repeat - 1) % len(cases)
            for case in cases[offset:] + cases[:offset]:
                order = execution_order(repeat)
                report["execution_order"].append({"repeat": repeat, "case": case, "versions": order})
                for version in order:
                    binary = None if version == "reference" else getattr(args, version + "_binary")
                    row = measure(case, version, binary, workloads, args.output / case / f"repeat-{repeat}" / version,
                                  args.duration, args.warmup, repeat, args.batch_threads, profile=args.profile)
                    report["runs"].append(row)
                    report["assessment"] = compare(report["runs"])
                    atomic_json(args.output / "summary.json", report)
                    print(json.dumps({"case": case, "repeat": repeat, "version": version, "failures": row["failures"]}), flush=True)
                    if row["final_scheduler_state"] != "disabled":
                        raise RuntimeError("cleanup did not restore the default scheduler; stopping comparisons")
                    if "unexpected_native_scheduler_exit" in row["failures"]:
                        raise RuntimeError("unexpected scheduler exit; evidence saved, repair required before more trials")
        report["status"] = report["assessment"]["status"]
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        workloads.close()
        os.sched_setaffinity(0, set(allowed))
        report["cleanup"] = workloads.cleanup
        report["final_scheduler_state"] = ScxController().state()
        if workloads.cleanup["status"] != "passed" or report["final_scheduler_state"] != "disabled":
            report["status"] = "failed"
        atomic_json(args.output / "summary.json", report)
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
