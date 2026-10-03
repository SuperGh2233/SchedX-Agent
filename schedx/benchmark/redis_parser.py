from __future__ import annotations

import re


def parse_redis_benchmark_output(output: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    throughput = re.search(
        r"throughput summary:\s*([0-9.]+)\s+requests per second", output
    )
    if throughput:
        metrics["requests_per_sec"] = float(throughput.group(1))
        metrics["qps"] = float(throughput.group(1))

    completed = re.search(
        r"([0-9]+)\s+requests completed in\s+([0-9.]+)\s+seconds", output
    )
    if completed:
        metrics["requests"] = float(completed.group(1))
        metrics["elapsed_seconds"] = float(completed.group(2))

    latency = re.search(
        r"latency summary \(msec\):\s*"
        r"avg\s+min\s+p50\s+p95\s+p99\s+max\s*"
        r"([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+"
        r"([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)",
        output,
        re.DOTALL,
    )
    if latency:
        names = (
            "latency_avg_ms",
            "latency_min_ms",
            "p50_ms",
            "p95_ms",
            "p99_ms",
            "latency_max_ms",
        )
        metrics.update(
            {name: float(value) for name, value in zip(names, latency.groups())}
        )

    # Compatibility with quiet output used by the original single-run command.
    qps_values: list[float] = []
    for line in output.splitlines():
        qps = re.search(r"([0-9.]+)\s+requests per second", line)
        if qps:
            qps_values.append(float(qps.group(1)))
        for percentile in ("p50", "p95", "p99"):
            value = re.search(rf"{percentile}=([0-9.]+)\s*msec", line)
            if value and f"{percentile}_ms" not in metrics:
                metrics[f"{percentile}_ms"] = float(value.group(1))
    if "requests_per_sec" not in metrics and qps_values:
        metrics["requests_per_sec"] = round(sum(qps_values) / len(qps_values), 2)
        metrics["qps"] = metrics["requests_per_sec"]
    for percentile in ("p50", "p95", "p99"):
        if f"{percentile}_ms" in metrics:
            metrics[f"{percentile}_latency_ms"] = metrics[f"{percentile}_ms"]
    return metrics
