from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Mapping

from schedx.policies.repository import PolicyRepository


MODE_TO_EXPERT = {
    "latency_first": "latency_guard",
    "throughput_first": "throughput_boost",
    "isolate_background": "background_isolation",
    "balanced": "balanced",
}

MODE_TO_GROUP = {
    "latency_first": "latency_sensitive",
    "throughput_first": "batch_compute",
    "isolate_background": "background_noise",
}


@dataclass(frozen=True)
class RouteDecision:
    expert_id: str
    mode: str
    confidence: float
    scores: dict[str, float]
    switched: bool
    reason: str
    timestamp: float

    def to_dict(self) -> dict[str, object]:
        return {
            "expert_id": self.expert_id,
            "mode": self.mode,
            "confidence": self.confidence,
            "scores": self.scores,
            "switched": self.switched,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


class SchedulerRouter:
    """Stabilize workload observations before selecting an expert policy."""

    def __init__(
        self,
        repository: PolicyRepository,
        *,
        window_size: int = 6,
        decay: float = 0.75,
        confidence_threshold: float = 0.40,
        cooldown_seconds: float = 6.0,
    ) -> None:
        if window_size < 1:
            raise ValueError("window_size must be at least 1")
        if not 0.0 < decay <= 1.0:
            raise ValueError("decay must be in (0, 1]")
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be in [0, 1]")
        if cooldown_seconds < 0.0:
            raise ValueError("cooldown_seconds cannot be negative")
        self.repository = repository
        self.decay = decay
        self.confidence_threshold = confidence_threshold
        self.cooldown_seconds = cooldown_seconds
        self._history: deque[dict[str, float]] = deque(maxlen=window_size)
        self.current_expert: str | None = None
        self.last_switch_at: float | None = None

    def route(
        self,
        classification: dict,
        proposed_mode: str,
        proposed_confidence: float,
        *,
        expert_scores: Mapping[str, float] | None = None,
        now: float | None = None,
    ) -> RouteDecision:
        timestamp = time.monotonic() if now is None else float(now)
        observation = (
            self._normalize(expert_scores)
            if expert_scores is not None
            else self._scores_from_observation(
                classification, proposed_mode, proposed_confidence
            )
        )
        self._history.append(observation)
        scores = self._aggregate()
        candidate = max(scores, key=scores.get)
        candidate_confidence = scores[candidate]

        if candidate_confidence < self.confidence_threshold:
            selected = self.current_expert or "balanced"
            reason = "low_confidence_fallback"
            switched = False
        elif self.current_expert is None:
            selected = candidate
            reason = "initial_selection"
            switched = selected != "balanced"
            self.current_expert = selected
            self.last_switch_at = timestamp
        elif (
            candidate != self.current_expert
            and self.last_switch_at is not None
            and timestamp - self.last_switch_at < self.cooldown_seconds
        ):
            selected = self.current_expert
            reason = "cooldown_hold"
            switched = False
        elif candidate != self.current_expert:
            selected = candidate
            reason = "higher_weighted_score"
            switched = True
            self.current_expert = selected
            self.last_switch_at = timestamp
        else:
            selected = candidate
            reason = "stable_selection"
            switched = False

        if self.current_expert is None:
            self.current_expert = selected
            self.last_switch_at = timestamp
        expert = self.repository.get(selected)
        return RouteDecision(
            expert_id=selected,
            mode=expert.mode,
            confidence=scores.get(selected, 0.0),
            scores={key: round(value, 6) for key, value in scores.items()},
            switched=switched,
            reason=reason,
            timestamp=timestamp,
        )

    def _scores_from_observation(
        self,
        classification: dict,
        proposed_mode: str,
        proposed_confidence: float,
    ) -> dict[str, float]:
        scores = {
            expert.expert_id: 0.05 for expert in self.repository.list_experts()
        }
        groups = classification.get("groups", {})
        latency = len(groups.get("latency_sensitive", []))
        batch = len(groups.get("batch_compute", []))
        background = len(groups.get("background_noise", []))

        proposed_expert = MODE_TO_EXPERT.get(proposed_mode)
        if proposed_expert:
            scores[proposed_expert] += 0.45 * max(
                0.0, min(1.0, float(proposed_confidence))
            )
        if latency:
            scores["latency_guard"] += 0.45
        if batch:
            scores["throughput_boost"] += 0.45
        if background:
            scores["background_isolation"] += 0.35
        if latency and background:
            scores["latency_guard"] += 0.35
        active_types = sum(bool(value) for key, value in groups.items() if key != "unknown")
        if active_types == 0:
            scores["balanced"] += 0.80
        elif active_types > 1:
            scores["balanced"] += 0.10
        return self._normalize(scores)

    def _aggregate(self) -> dict[str, float]:
        totals = {expert.expert_id: 0.0 for expert in self.repository.list_experts()}
        total_weight = 0.0
        count = len(self._history)
        for index, sample in enumerate(self._history):
            age = count - index - 1
            weight = self.decay**age
            total_weight += weight
            for expert_id in totals:
                totals[expert_id] += sample.get(expert_id, 0.0) * weight
        if total_weight:
            totals = {key: value / total_weight for key, value in totals.items()}
        return self._normalize(totals)

    def _normalize(self, values: Mapping[str, float]) -> dict[str, float]:
        allowed = {expert.expert_id for expert in self.repository.list_experts()}
        normalized = {
            expert_id: max(0.0, float(values.get(expert_id, 0.0)))
            for expert_id in allowed
        }
        total = sum(normalized.values())
        if total <= 0.0:
            return {
                expert_id: 1.0 if expert_id == "balanced" else 0.0
                for expert_id in sorted(allowed)
            }
        return {
            expert_id: value / total
            for expert_id, value in sorted(normalized.items())
        }


def target_for_mode(mode: str, classification: dict, fallback: str = "") -> str:
    groups = classification.get("groups", {})
    group_name = MODE_TO_GROUP.get(mode)
    candidates = list(groups.get(group_name, [])) if group_name else []
    if not candidates:
        for fallback_group in (
            "latency_sensitive",
            "batch_compute",
            "background_noise",
        ):
            candidates = list(groups.get(fallback_group, []))
            if candidates:
                break
    if not candidates:
        return fallback
    selected = max(
        candidates,
        key=lambda process: float(process.get("cpu_percent", 0.0) or 0.0),
    )
    return str(selected.get("comm", "")) or fallback

