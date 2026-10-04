from __future__ import annotations

import math
import re


def parse_wrk_output(output: str) -> dict[str, object]:
    metrics: dict[str, object] = {}

    completed = re.search(r"(\d+)\s+requests in\s+([0-9.]+)(us|ms|s|m|h)\s*,", output)
    if completed:
        metrics["requests_completed"] = int(completed.group(1))
        metrics["elapsed_seconds"] = (
            float(completed.group(2))
            * {
                "us": 0.000001,
                "ms": 0.001,
                "s": 1,
                "m": 60,
                "h": 3600,
            }[completed.group(3)]
        )

    socket_errors = re.search(
        r"Socket errors:[ \t]*connect (\d+), read (\d+), write (\d+), timeout (\d+)[ \t]*\r?$",
        output,
        re.MULTILINE,
    )
    response_errors = re.search(
        r"Non-2xx or 3xx responses:[ \t]*(\d+)[ \t]*\r?$", output, re.MULTILINE
    )
    if completed or socket_errors:
        metrics["socket_errors"] = dict(
            zip(
                ("connect", "read", "write", "timeout"),
                map(int, socket_errors.groups()) if socket_errors else (0, 0, 0, 0),
            )
        )
    if completed or response_errors:
        metrics["non_success_responses"] = (
            int(response_errors.group(1)) if response_errors else 0
        )
    if ("Socket errors:" in output and not socket_errors) or (
        "Non-2xx or 3xx responses:" in output and not response_errors
    ):
        metrics["request_quality_invalid"] = True

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

    if completed and not metrics.get("request_quality_invalid"):
        count = metrics["requests_completed"]
        elapsed = metrics["elapsed_seconds"]
        errors = metrics["non_success_responses"]
        if not math.isfinite(elapsed) or elapsed <= 0 or errors > count:
            metrics["request_quality_invalid"] = True
        else:
            successful_rate = (count - errors) / elapsed
            socket_rate = sum(metrics["socket_errors"].values()) / elapsed
            if not math.isfinite(successful_rate) or not math.isfinite(socket_rate):
                metrics["request_quality_invalid"] = True
            else:
                metrics["successful_requests_per_sec"] = successful_rate
                metrics["response_error_rate"] = errors / count if count else 0.0
                metrics["socket_errors_per_second"] = socket_rate

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
