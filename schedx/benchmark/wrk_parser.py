from __future__ import annotations

import re


def parse_wrk_output(output: str) -> dict[str, float | str]:
    metrics: dict[str, float | str] = {}

    latency = re.search(
        r"Latency\s+([0-9.]+)(us|ms|s)\s+([0-9.]+)(us|ms|s)\s+([0-9.]+)(us|ms|s)",
        output,
    )
    if latency:
        metrics["latency_avg_ms"] = _to_ms(float(latency.group(1)), latency.group(2))
        metrics["latency_stdev_ms"] = _to_ms(float(latency.group(3)), latency.group(4))
        metrics["latency_max_ms"] = _to_ms(float(latency.group(5)), latency.group(6))

    req_sec = re.search(
        r"Req/Sec\s+([0-9.]+)([kKmMgG]?)\s+([0-9.]+)([kKmMgG]?)",
        output,
    )
    if req_sec:
        metrics["req_per_sec_avg"] = _scale(float(req_sec.group(1)), req_sec.group(2))
        metrics["req_per_sec_stdev"] = _scale(float(req_sec.group(3)), req_sec.group(4))

    requests = re.search(r"Requests/sec:\s*([0-9.]+)", output)
    if requests:
        metrics["requests_per_sec"] = float(requests.group(1))
        metrics["qps"] = float(requests.group(1))

    transfer = re.search(r"Transfer/sec:\s*([^\s]+)", output)
    if transfer:
        metrics["transfer_sec"] = transfer.group(1)

    for percentile in ("50", "75", "90", "99"):
        match = re.search(rf"\s{percentile}%\s+([0-9.]+)(us|ms|s)", output)
        if match:
            value = _to_ms(float(match.group(1)), match.group(2))
            metrics[f"p{percentile}_ms"] = value
            metrics[f"p{percentile}_latency_ms"] = value

    if "latency_avg_ms" in metrics:
        metrics["avg_latency_ms"] = metrics["latency_avg_ms"]

    return metrics


def _to_ms(value: float, unit: str) -> float:
    if unit == "us":
        return round(value / 1000.0, 4)
    if unit == "s":
        return round(value * 1000.0, 4)
    return value


def _scale(value: float, suffix: str) -> float:
    suffix = suffix.lower()
    if suffix == "k":
        return value * 1000.0
    if suffix == "m":
        return value * 1000_000.0
    if suffix == "g":
        return value * 1000_000_000.0
    return value
