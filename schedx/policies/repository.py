from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_EXPERT_MODES = {
    "latency_first",
    "throughput_first",
    "isolate_background",
    "balanced",
}


@dataclass(frozen=True)
class ExpertPolicy:
    expert_id: str
    mode: str
    description: str
    workload_types: tuple[str, ...]
    parameters: dict[str, Any]

    def __post_init__(self) -> None:
        if self.mode not in ALLOWED_EXPERT_MODES:
            raise ValueError(f"unsupported expert mode: {self.mode}")


@dataclass(frozen=True)
class PolicyOutcome:
    observations: int = 0
    accepts: int = 0
    rejects: int = 0
    last_accepted: bool | None = None
    last_metrics: dict[str, float] = field(default_factory=dict)
    last_reason: str = ""
    last_updated: str = ""


def builtin_experts() -> tuple[ExpertPolicy, ...]:
    return (
        ExpertPolicy(
            expert_id="latency_guard",
            mode="latency_first",
            description="Protect latency-sensitive services from batch and background interference.",
            workload_types=("latency_sensitive", "background_noise", "mixed"),
            parameters={
                "cpu_weight": 10000,
                "cpu_weight_bg": 25,
                "cpu_max_bg": "25000 100000",
                "nice_target": -10,
                "nice_bg": 10,
            },
        ),
        ExpertPolicy(
            expert_id="throughput_boost",
            mode="throughput_first",
            description="Favor sustained batch-compute throughput.",
            workload_types=("batch_compute",),
            parameters={"cpu_weight": 9000, "cpu_max": "max 100000"},
        ),
        ExpertPolicy(
            expert_id="background_isolation",
            mode="isolate_background",
            description="Constrain explicitly matched background interference.",
            workload_types=("background_noise",),
            parameters={
                "cpu_weight_bg": 50,
                "cpu_max_bg": "25000 100000",
                "nice_bg": 10,
            },
        ),
        ExpertPolicy(
            expert_id="balanced",
            mode="balanced",
            description="Conservative fallback when no workload pattern is confident.",
            workload_types=("unknown", "mixed"),
            parameters={"cpu_weight": 500},
        ),
    )


class PolicyRepository:
    """Allowlisted expert catalog with JSON-backed outcome history."""

    VERSION = 1

    def __init__(self, path: Path = Path(".schedx/policy_repository.json")) -> None:
        self.path = path
        self.load_error = ""
        self._experts = {expert.expert_id: expert for expert in builtin_experts()}
        self._outcomes = {
            expert_id: PolicyOutcome() for expert_id in self._experts
        }
        self._load()

    def list_experts(self) -> list[ExpertPolicy]:
        return [self._experts[key] for key in sorted(self._experts)]

    def get(self, expert_id: str) -> ExpertPolicy:
        try:
            return self._experts[expert_id]
        except KeyError as exc:
            raise KeyError(f"unknown expert: {expert_id}") from exc

    def outcome_for(self, expert_id: str) -> PolicyOutcome:
        self.get(expert_id)
        return self._outcomes[expert_id]

    def record_outcome(
        self,
        expert_id: str,
        *,
        accepted: bool,
        metrics: dict[str, float] | None = None,
        reason: str = "",
    ) -> PolicyOutcome:
        self.get(expert_id)
        previous = self._outcomes[expert_id]
        outcome = PolicyOutcome(
            observations=previous.observations + 1,
            accepts=previous.accepts + int(accepted),
            rejects=previous.rejects + int(not accepted),
            last_accepted=accepted,
            last_metrics=dict(metrics or {}),
            last_reason=reason,
            last_updated=datetime.now(timezone.utc).isoformat(),
        )
        self._outcomes[expert_id] = outcome
        self._save()
        return outcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.VERSION,
            "path": str(self.path),
            "load_error": self.load_error,
            "experts": [
                {
                    **asdict(expert),
                    "outcome": asdict(self._outcomes[expert.expert_id]),
                }
                for expert in self.list_experts()
            ],
        }

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != self.VERSION:
                raise ValueError("unsupported policy repository version")
            outcomes = payload.get("outcomes", {})
            if not isinstance(outcomes, dict):
                raise ValueError("policy outcomes must be an object")
            for expert_id in self._experts:
                raw = outcomes.get(expert_id)
                if not isinstance(raw, dict):
                    continue
                self._outcomes[expert_id] = PolicyOutcome(
                    observations=int(raw.get("observations", 0)),
                    accepts=int(raw.get("accepts", 0)),
                    rejects=int(raw.get("rejects", 0)),
                    last_accepted=raw.get("last_accepted"),
                    last_metrics={
                        str(key): float(value)
                        for key, value in dict(raw.get("last_metrics", {})).items()
                    },
                    last_reason=str(raw.get("last_reason", "")),
                    last_updated=str(raw.get("last_updated", "")),
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.load_error = str(exc)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.VERSION,
            "experts": [asdict(expert) for expert in self.list_experts()],
            "outcomes": {
                expert_id: asdict(outcome)
                for expert_id, outcome in sorted(self._outcomes.items())
            },
        }
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(self.path)

