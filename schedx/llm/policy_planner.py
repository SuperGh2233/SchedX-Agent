from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from schedx.agent.decision import Decision
from schedx.llm.client import LLMClient, LLMError

ALLOWED_MODES = {
    "latency_first",
    "throughput_first",
    "balanced",
    "isolate_background",
}

PARAMETER_LIMITS: dict[str, tuple[int, int] | set[str]] = {
    "cpu_weight": (1, 10000),
    "cpu_weight_bg": (1, 10000),
    "nice_target": (-20, 19),
    "nice_bg": (-20, 19),
    "cpu_max_bg": {
        "10000 100000",
        "15000 100000",
        "25000 100000",
        "50000 100000",
        "80000 100000",
        "max",
    },
}


class LLMPolicyPlanner:
    """Ask an LLM for a constrained policy proposal and validate it locally."""

    def __init__(self, client: LLMClient | None = None) -> None:
        self.client = client or LLMClient()

    def propose(
        self,
        classification: dict[str, Any],
        pressure: dict[str, Any],
        topology: dict[str, Any],
        rule_decision: Decision,
        scx_status: dict[str, Any] | None = None,
    ) -> Decision:
        if not self.client.is_configured():
            raise LLMError("LLM is not configured")
        system = """You are the policy-planning component of SchedX-Agent.
Return one strict JSON object only. You may recommend policy parameters, but
you cannot execute commands. Prefer safe, reversible changes. The local safety
validator will reject unsupported values.

Required JSON shape:
{
  "mode": "latency_first|throughput_first|balanced|isolate_background",
  "target": "existing process name or empty string",
  "parameters": {
    "cpu_weight": integer,
    "cpu_weight_bg": integer,
    "cpu_max_bg": "allowed quota string",
    "nice_target": integer,
    "nice_bg": integer
  },
  "reason": "brief explanation",
  "confidence": number between 0 and 1
}
Only include parameters needed by the selected policy."""
        data = {
            "classification": classification,
            "pressure": pressure,
            "topology": topology,
            "native_sched_ext": scx_status or {},
            "safe_rule_baseline": asdict(rule_decision),
        }
        proposal = self.client.chat_json(
            [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": "Choose a safe scheduling policy for this observation:\n"
                    + json.dumps(data, ensure_ascii=False),
                },
            ]
        )
        return self.validate(proposal, classification, rule_decision)

    def validate(
        self, proposal: dict[str, Any], classification: dict[str, Any], fallback: Decision
    ) -> Decision:
        mode = str(proposal.get("mode", ""))
        if mode not in ALLOWED_MODES:
            raise LLMError(f"unsupported LLM policy mode: {mode}")
        allowed_targets = {
            str(proc.get("comm", ""))
            for processes in classification.get("groups", {}).values()
            for proc in processes
            if proc.get("comm")
        }
        target = str(proposal.get("target", ""))
        if target and target not in allowed_targets:
            target = fallback.target

        raw_parameters = proposal.get("parameters", {})
        if not isinstance(raw_parameters, dict):
            raise LLMError("LLM policy parameters must be an object")
        parameters: dict[str, Any] = {}
        for key, value in raw_parameters.items():
            limits = PARAMETER_LIMITS.get(key)
            if limits is None:
                continue
            if isinstance(limits, tuple):
                value = int(value)
                if not limits[0] <= value <= limits[1]:
                    raise LLMError(f"{key} is outside the safe range")
            elif str(value) not in limits:
                raise LLMError(f"{key} is not an allowed value")
            parameters[key] = value

        return Decision(
            mode=mode,
            target=target or fallback.target,
            parameters={**fallback.parameters, **parameters},
            reason=f"DeepSeek V4 proposal: {str(proposal.get('reason', ''))[:500]}",
            confidence=max(0.0, min(1.0, float(proposal.get("confidence", 0.5)))),
        )
