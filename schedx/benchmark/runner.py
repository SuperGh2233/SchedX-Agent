from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Sequence

from schedx.benchmark.nginx import NginxBenchmarkConfig, NginxStressBenchmark
from schedx.benchmark.wrk_parser import parse_wrk_output


class BenchmarkRunner:
    SUPPORTED = {"nginx", "nginx-latency", "redis-latency", "batch-cpu"}
    VARIANTS = {"default", "schedx"}

    def run(
        self,
        name: str,
        output: Path = Path("results"),
        variant: str = "default",
        duration: int = 10,
        url: str = "http://127.0.0.1/",
        connections: int = 64,
        threads: int = 4,
        repeats: int = 3,
        stress_cpu: int = 4,
    ) -> dict:
        if name not in self.SUPPORTED:
            raise ValueError(f"unsupported benchmark: {name}")
        if variant not in self.VARIANTS:
            raise ValueError(f"unsupported benchmark variant: {variant}")

        if name == "nginx":
            return NginxStressBenchmark().run(
                NginxBenchmarkConfig(
                    url=url,
                    duration=duration,
                    connections=connections,
                    threads=threads,
                    repeats=repeats,
                    stress_cpu=stress_cpu,
                    output=output,
                )
            )

        output.mkdir(parents=True, exist_ok=True)
        started = int(time.time())
        if name == "nginx-latency":
            result = self._run_nginx(duration, url)
            file_name = f"nginx_{variant}.json"
        elif name == "redis-latency":
            result = self._run_redis(duration)
            file_name = f"redis_{variant}.json"
        else:
            result = self._run_batch(duration)
            file_name = f"batch_{variant}.json"

        result.update({"benchmark": name, "variant": variant, "timestamp": started})
        (output / file_name).write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    def _run_nginx(self, duration: int, url: str) -> dict:
        if not shutil.which("wrk"):
            return self._tool_missing("wrk", "Install wrk to collect nginx latency metrics.")
        command = ["wrk", "-t2", "-c64", f"-d{duration}s", "--latency", url]
        completed = self._run_command(command, timeout=duration + 15)
        metrics = parse_wrk_output(completed.stdout)
        return self._result_from_completed(completed, command, metrics)

    def _run_redis(self, duration: int) -> dict:
        if not shutil.which("redis-benchmark"):
            return self._tool_missing("redis-benchmark", "Install redis-benchmark to collect Redis metrics.")
        requests = max(duration, 1) * 10000
        command = ["redis-benchmark", "-q", "-n", str(requests), "-c", "64", "-t", "get,set"]
        completed = self._run_command(command, timeout=duration + 30)
        metrics = parse_redis_benchmark_output(completed.stdout)
        return self._result_from_completed(completed, command, metrics)

    def _run_batch(self, duration: int) -> dict:
        if not shutil.which("sysbench"):
            return self._tool_missing("sysbench", "Install sysbench to collect batch CPU metrics.")
        command = ["sysbench", "cpu", f"--time={duration}", "run"]
        started = time.perf_counter()
        completed = self._run_command(command, timeout=duration + 30)
        elapsed = time.perf_counter() - started
        metrics = parse_sysbench_output(completed.stdout)
        metrics.setdefault("elapsed_seconds", round(elapsed, 3))
        return self._result_from_completed(completed, command, metrics)

    def _run_command(self, command: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _result_from_completed(
        self,
        completed: subprocess.CompletedProcess[str],
        command: Sequence[str],
        metrics: dict[str, float],
    ) -> dict:
        return {
            "status": "ok" if completed.returncode == 0 else "failed",
            "command": list(command),
            "returncode": completed.returncode,
            "metrics": metrics,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }

    def _tool_missing(self, tool: str, message: str) -> dict:
        return {
            "status": "tool_missing",
            "tool": tool,
            "message": message,
            "metrics": {},
        }
def parse_redis_benchmark_output(output: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    qps_values: list[float] = []
    for line in output.splitlines():
        qps = re.search(r"([0-9.]+)\s+requests per second", line)
        if qps:
            qps_values.append(float(qps.group(1)))
        p50 = re.search(r"p50=([0-9.]+)\s*msec", line)
        p95 = re.search(r"p95=([0-9.]+)\s*msec", line)
        p99 = re.search(r"p99=([0-9.]+)\s*msec", line)
        if p50:
            metrics["p50_latency_ms"] = float(p50.group(1))
        if p95:
            metrics["p95_latency_ms"] = float(p95.group(1))
        if p99:
            metrics["p99_latency_ms"] = float(p99.group(1))
    if qps_values:
        metrics["qps"] = round(sum(qps_values) / len(qps_values), 2)
    return metrics


def parse_sysbench_output(output: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    total = re.search(r"total time:\s*([0-9.]+)s", output)
    events = re.search(r"total number of events:\s*([0-9]+)", output)
    eps = re.search(r"events per second:\s*([0-9.]+)", output)
    if total:
        metrics["elapsed_seconds"] = float(total.group(1))
    if events:
        metrics["events"] = float(events.group(1))
    if eps:
        metrics["events_per_second"] = float(eps.group(1))
    return metrics
