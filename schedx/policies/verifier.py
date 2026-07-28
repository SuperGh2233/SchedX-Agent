from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass


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
        min_background_retention: float = 0.25,
        min_p99_improvement_percent: float | None = None,
    ) -> None:
        if max_regression_percent < 0:
            raise ValueError("max_regression_percent cannot be negative")
        if not 0.0 <= min_background_share <= 1.0:
            raise ValueError("min_background_share must be in [0, 1]")
        if not 0.0 <= min_background_retention <= 1.0:
            raise ValueError("min_background_retention must be in [0, 1]")
        if (
            min_p99_improvement_percent is not None
            and min_p99_improvement_percent < 0.0
        ):
            raise ValueError("min_p99_improvement_percent cannot be negative")
        self.max_regression_percent = max_regression_percent
        self.min_background_share = min_background_share
        self.min_background_retention = min_background_retention
        self.min_p99_improvement_percent = min_p99_improvement_percent

    def evaluate(
        self,
        mode: str,
        baseline: object,
        candidate: object,
        *,
        nr_rejected: object = 0,
        background_share: object = None,
        background_retention: object = None,
        background_expected: bool = False,
    ) -> CanaryVerdict:
        reasons: list[str] = []
        deltas: dict[str, float] = {}

        if not isinstance(baseline, Mapping) or not isinstance(candidate, Mapping):
            return CanaryVerdict(
                False, "rejected", ["invalid_metric_payload"], deltas
            )

        baseline_p99, baseline_p99_invalid = self._metric(
            baseline, "p99_ms", "p99_latency_ms", "mean_p99_ms"
        )
        candidate_p99, candidate_p99_invalid = self._metric(
            candidate, "p99_ms", "p99_latency_ms", "mean_p99_ms"
        )
        baseline_rps, baseline_rps_invalid = self._metric(
            baseline, "requests_per_sec", "rps", "mean_requests_per_sec", "qps"
        )
        candidate_rps, candidate_rps_invalid = self._metric(
            candidate, "requests_per_sec", "rps", "mean_requests_per_sec", "qps"
        )

        rejected_count, rejected_invalid = self._nonnegative_number(nr_rejected)
        share, share_invalid = self._share(background_share)
        retention, retention_invalid = self._retention(background_retention)
        if any(
            (
                baseline_p99_invalid,
                candidate_p99_invalid,
                baseline_rps_invalid,
                candidate_rps_invalid,
                rejected_invalid,
                share_invalid,
                retention_invalid,
            )
        ):
            return CanaryVerdict(
                False, "rejected", ["invalid_metric_value"], deltas
            )

        p99_delta = self._percent_change(baseline_p99, candidate_p99)
        rps_delta = self._percent_change(baseline_rps, candidate_rps)
        if p99_delta is not None:
            deltas["p99_percent"] = p99_delta
        if rps_delta is not None:
            deltas["requests_per_sec_percent"] = rps_delta

        if rejected_count is not None and rejected_count > 0:
            reasons.append("sched_ext_rejected_tasks")
        if share is not None and share < self.min_background_share:
            reasons.append("background_starvation")
        if retention is not None:
            deltas["background_retention_percent"] = round(retention * 100.0, 6)
            if retention < self.min_background_retention:
                reasons.append("background_progress_regression")
        elif background_expected:
            reasons.append("missing_background_progress")

        objective_available = False
        if mode in {"latency_first", "isolate_background"}:
            objective_available = p99_delta is not None
            if p99_delta is not None and p99_delta > self.max_regression_percent:
                reasons.append("p99_regression")
            if (
                p99_delta is not None
                and self.min_p99_improvement_percent is not None
                and -p99_delta < self.min_p99_improvement_percent
            ):
                reasons.append("insufficient_p99_improvement")
            if rps_delta is not None and rps_delta < -self.max_regression_percent:
                reasons.append("throughput_regression")
        elif mode == "throughput_first":
            objective_available = rps_delta is not None
            if rps_delta is not None and rps_delta < -self.max_regression_percent:
                reasons.append("throughput_regression")
            if (
                p99_delta is not None
                and p99_delta > self.max_regression_percent * 2
            ):
                reasons.append("p99_regression")
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
        values: Mapping[str, object], *keys: str
    ) -> tuple[float | None, bool]:
        for key in keys:
            if key not in values:
                continue
            value = values[key]
            if value is None:
                continue
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return None, True
            if not math.isfinite(parsed) or parsed < 0.0:
                return None, True
            return parsed, False
        return None, False

    @staticmethod
    def _nonnegative_number(value: object) -> tuple[float | None, bool]:
        if value is None:
            return 0.0, False
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None, True
        if not math.isfinite(parsed) or parsed < 0.0:
            return None, True
        return parsed, False

    @staticmethod
    def _share(value: object) -> tuple[float | None, bool]:
        if value is None:
            return None, False
        parsed, invalid = CanaryVerifier._nonnegative_number(value)
        if invalid or parsed is None or parsed > 1.0:
            return None, True
        return parsed, False

    @staticmethod
    def _retention(value: object) -> tuple[float | None, bool]:
        """Return a non-negative progress ratio; values above one are valid gains."""
        if value is None:
            return None, False
        return CanaryVerifier._nonnegative_number(value)

    @staticmethod
    def _percent_change(before: float | None, after: float | None) -> float | None:
        if before is None or after is None or before == 0:
            return None
        return round((after - before) / before * 100.0, 6)
