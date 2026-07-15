from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class CanaryVerdict:
    accepted: bool
    status: str
    reasons: list[str]
    deltas: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "status": self.status,
            "reasons": self.reasons,
            "deltas": self.deltas,
        }


class CanaryVerifier:
    """Evaluate candidate policy metrics without performing system actions."""

    def __init__(
        self,
        *,
        max_regression_percent: float = 5.0,
        min_background_share: float = 0.08,
    ) -> None:
        if max_regression_percent < 0:
            raise ValueError("max_regression_percent cannot be negative")
        if not 0.0 <= min_background_share <= 1.0:
            raise ValueError("min_background_share must be in [0, 1]")
        self.max_regression_percent = max_regression_percent
        self.min_background_share = min_background_share

    def evaluate(
        self,
        mode: str,
        baseline: Mapping[str, float | int | None],
        candidate: Mapping[str, float | int | None],
        *,
        nr_rejected: int = 0,
        background_share: float | None = None,
    ) -> CanaryVerdict:
        reasons: list[str] = []
        deltas: dict[str, float] = {}

        baseline_p99 = self._metric(
            baseline, "p99_ms", "p99_latency_ms", "mean_p99_ms"
        )
        candidate_p99 = self._metric(
            candidate, "p99_ms", "p99_latency_ms", "mean_p99_ms"
        )
        baseline_rps = self._metric(
            baseline, "requests_per_sec", "rps", "mean_requests_per_sec", "qps"
        )
        candidate_rps = self._metric(
            candidate, "requests_per_sec", "rps", "mean_requests_per_sec", "qps"
        )

        p99_delta = self._percent_change(baseline_p99, candidate_p99)
        rps_delta = self._percent_change(baseline_rps, candidate_rps)
        if p99_delta is not None:
            deltas["p99_percent"] = p99_delta
        if rps_delta is not None:
            deltas["requests_per_sec_percent"] = rps_delta

        if int(nr_rejected) > 0:
            reasons.append("sched_ext_rejected_tasks")
        if (
            background_share is not None
            and float(background_share) < self.min_background_share
        ):
            reasons.append("background_starvation")

        objective_available = False
        if mode in {"latency_first", "isolate_background"}:
            objective_available = p99_delta is not None
            if p99_delta is not None and p99_delta > self.max_regression_percent:
                reasons.append("p99_regression")
        elif mode == "throughput_first":
            objective_available = rps_delta is not None
            if rps_delta is not None and rps_delta < -self.max_regression_percent:
                reasons.append("throughput_regression")
        else:
            objective_available = p99_delta is not None or rps_delta is not None
            severe = self.max_regression_percent * 2
            if p99_delta is not None and p99_delta > severe:
                reasons.append("p99_regression")
            if rps_delta is not None and rps_delta < -severe:
                reasons.append("throughput_regression")

        if reasons:
            return CanaryVerdict(False, "rejected", reasons, deltas)
        if not objective_available:
            return CanaryVerdict(
                True,
                "inconclusive",
                ["missing_objective_metrics"],
                deltas,
            )
        return CanaryVerdict(True, "accepted", [], deltas)

    @staticmethod
    def _metric(
        values: Mapping[str, float | int | None], *keys: str
    ) -> float | None:
        for key in keys:
            value = values.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _percent_change(before: float | None, after: float | None) -> float | None:
        if before is None or after is None or before == 0:
            return None
        return round((after - before) / before * 100.0, 6)

