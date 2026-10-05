from __future__ import annotations

import hashlib
import json

from schedx.agent.decision import Decision, DecisionEngine


def trial_context_key(decision: Decision, data: dict, generation: int) -> str | None:
    """Identify an attempt without carrying feedback across workload identities.

    CPU utilization is deliberately not a key: small sampling fluctuations do
    not constitute a different workload. Pressure bands, PID start times,
    classification, policy parameters and measurement requirements do.
    """
    groups = data.get("classification", {}).get("groups", {})
    processes = sorted(
        (name, int(row["pid"]), row.get("start_time"), row.get("comm"))
        for name, rows in groups.items() for row in rows if row.get("pid") is not None
    )
    if not processes or any(isinstance(row[2], bool) or not isinstance(row[2], int) or row[2] <= 0
                            for row in processes):
        return None
    engine = DecisionEngine()
    pressure = data.get("snapshot", {}).get("pressure", {})
    payload = {
        "mode": decision.mode, "target": decision.target, "parameters": decision.parameters,
        "processes": processes, "scope": sorted(data.get("scope_pids") or []),
        "pressure": [engine._cpu_pressure_level(pressure), engine._mem_pressure_level(pressure)],
        "canary_config": data.get("canary_config"), "error_limits": data.get("canary_error_limits"),
        "retention": data.get("canary_min_background_retention", 0.25),
        "improvement": data.get("canary_min_p99_improvement"), "generation": generation,
    }
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()
