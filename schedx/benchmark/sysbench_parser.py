from __future__ import annotations

import re


def parse_sysbench_output(output: str) -> dict[str, float]:
    metrics: dict[str, float] = {}
    total = re.search(r"total time:\s*([0-9.]+)s", output)
    events = re.search(r"total number of events:\s*([0-9]+)", output)
    eps = re.search(r"events per second:\s*([0-9.]+)", output)
    p95 = re.search(r"95th percentile:\s*([0-9.]+)", output)
    if total:
        metrics["elapsed_seconds"] = float(total.group(1))
    if events:
        metrics["events"] = float(events.group(1))
    if eps:
        metrics["events_per_second"] = float(eps.group(1))
    if p95:
        metrics["latency_p95_ms"] = float(p95.group(1))
    return metrics
