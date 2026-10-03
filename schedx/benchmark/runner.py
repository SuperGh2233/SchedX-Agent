from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Sequence

from schedx.benchmark.ablation import NginxAblationBenchmark, NginxAblationConfig
from schedx.benchmark.batch import BatchThroughputBenchmark, BatchThroughputConfig
from schedx.benchmark.nginx import NginxBenchmarkConfig, NginxStressBenchmark
from schedx.benchmark.redis import RedisBenchmarkConfig, RedisMixedBenchmark
from schedx.benchmark.redis_parser import parse_redis_benchmark_output
from schedx.benchmark.scx_comparison import (
    ScxComparisonConfig,
    parse_scheduler_list,
    run_scx_comparison,
)
from schedx.benchmark.sysbench_parser import parse_sysbench_output
from schedx.benchmark.wrk_parser import parse_wrk_output


class BenchmarkRunner:
    SUPPORTED = {
        "nginx",
        "nginx-ablation",
        "nginx-latency",
        "redis",
        "redis-latency",
        "batch-cpu",
        "batch-throughput",
        "scx-compare",
    }
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
        warmup: int = 2,
        minimum_background_retention_percent: float = 25.0,
        schedulers: str | Sequence[str] | None = None,
        redis_host: str = "127.0.0.1",
        redis_port: int = 6379,
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

        if name == "nginx-ablation":
            return NginxAblationBenchmark().run(
                NginxAblationConfig(
                    url=url,
                    duration=duration,
                    connections=connections,
                    threads=threads,
                    repeats=repeats,
                    stress_cpu=stress_cpu,
                    warmup=warmup,
                    minimum_background_retention_percent=minimum_background_retention_percent,
                    output=output,
                )
            )

        if name == "batch-throughput":
            return BatchThroughputBenchmark().run(
                BatchThroughputConfig(
                    duration=duration,
                    threads=threads,
                    repeats=repeats,
                    stress_cpu=stress_cpu,
                    warmup=warmup,
                    minimum_background_retention_percent=minimum_background_retention_percent,
                    output=output,
                )
            )

        if name == "scx-compare":
            return run_scx_comparison(
                ScxComparisonConfig(
                    schedulers=parse_scheduler_list(schedulers)[1:],
                    url=url,
                    duration=duration,
                    connections=connections,
                    threads=threads,
                    repeats=repeats,
                    stress_cpu=stress_cpu,
                    warmup=warmup,
                    minimum_background_retention_percent=minimum_background_retention_percent,
                    output=output,
                )
            )

        if name == "redis":
            return RedisMixedBenchmark().run(
                RedisBenchmarkConfig(
                    host=redis_host,
                    port=redis_port,
                    duration=duration,
                    connections=connections,
                    threads=threads,
                    repeats=repeats,
                    stress_cpu=stress_cpu,
                    warmup=warmup,
                    minimum_background_retention_percent=minimum_background_retention_percent,
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
            return self._tool_missing(
                "wrk", "Install wrk to collect nginx latency metrics."
            )
        command = ["wrk", "-t2", "-c64", f"-d{duration}s", "--latency", url]
        completed = self._run_command(command, timeout=duration + 15)
        metrics = parse_wrk_output(completed.stdout)
        return self._result_from_completed(completed, command, metrics)

    def _run_redis(self, duration: int) -> dict:
        if not shutil.which("redis-benchmark"):
            return self._tool_missing(
                "redis-benchmark", "Install redis-benchmark to collect Redis metrics."
            )
        requests = max(duration, 1) * 10000
        command = [
            "redis-benchmark",
            "-q",
            "-n",
            str(requests),
            "-c",
            "64",
            "-t",
            "get,set",
        ]
        completed = self._run_command(command, timeout=duration + 30)
        metrics = parse_redis_benchmark_output(completed.stdout)
        return self._result_from_completed(completed, command, metrics)

    def _run_batch(self, duration: int) -> dict:
        if not shutil.which("sysbench"):
            return self._tool_missing(
                "sysbench", "Install sysbench to collect batch CPU metrics."
            )
        command = ["sysbench", "cpu", f"--time={duration}", "run"]
        started = time.perf_counter()
        completed = self._run_command(command, timeout=duration + 30)
        elapsed = time.perf_counter() - started
        metrics = parse_sysbench_output(completed.stdout)
        metrics.setdefault("elapsed_seconds", round(elapsed, 3))
        return self._result_from_completed(completed, command, metrics)

    def _run_command(
        self, command: Sequence[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
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


