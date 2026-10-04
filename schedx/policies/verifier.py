from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Real


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
        max_response_error_rate: float = 0.0,
        max_response_error_increase: float = 0.0,
        max_socket_errors_per_second: float = 0.0,
        max_socket_error_increase: float = 0.0,
    ) -> None:
        self.max_regression_percent = self._threshold(
            "max_regression_percent", max_regression_percent
        )
        self.min_background_share = self._threshold(
            "min_background_share", min_background_share, upper=1.0
        )
        self.min_background_retention = self._threshold(
            "min_background_retention", min_background_retention, upper=1.0
        )
        self.min_p99_improvement_percent = (
            self._threshold("min_p99_improvement_percent", min_p99_improvement_percent)
            if min_p99_improvement_percent is not None
            else None
        )
        self.max_response_error_rate = self._threshold(
            "max_response_error_rate", max_response_error_rate, upper=1.0
        )
        self.max_response_error_increase = self._threshold(
            "max_response_error_increase", max_response_error_increase, upper=1.0
        )
        self.max_socket_errors_per_second = self._threshold(
            "max_socket_errors_per_second", max_socket_errors_per_second
        )
        self.max_socket_error_increase = self._threshold(
            "max_socket_error_increase", max_socket_error_increase
        )

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
        error_metrics_expected: bool = False,
    ) -> CanaryVerdict:
        reasons: list[str] = []
        deltas: dict[str, float] = {}

        if not isinstance(baseline, Mapping) or not isinstance(candidate, Mapping):
            return CanaryVerdict(False, "rejected", ["invalid_metric_payload"], deltas)

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
            return CanaryVerdict(False, "rejected", ["invalid_metric_value"], deltas)

        before_quality = self._request_quality(baseline)
        after_quality = self._request_quality(candidate)
        if before_quality["invalid"] or after_quality["invalid"]:
            return CanaryVerdict(False, "rejected", ["invalid_request_quality"], deltas)
        quality_required = error_metrics_expected or bool(
            before_quality["known"] or after_quality["known"]
        )
        quality_complete = before_quality["complete"] and after_quality["complete"]
        response_count = after_quality["response_errors"]
        socket_count = after_quality["socket_errors"]
        if response_count and self.max_response_error_rate == 0:
            reasons.append("response_errors")
        if socket_count and self.max_socket_errors_per_second == 0:
            reasons.append("socket_errors")
        if after_quality["complete"]:
            if after_quality["completed"] == 0:
                reasons.append("no_completed_requests")
            if after_quality["response_error_rate"] > self.max_response_error_rate:
                if "response_errors" not in reasons:
                    reasons.append("response_errors")
            if (
                after_quality["socket_errors_per_second"]
                > self.max_socket_errors_per_second
            ):
                if "socket_errors" not in reasons:
                    reasons.append("socket_errors")
        if quality_complete:
            # Completed HTTP responses and socket events have distinct denominators.
            response_increase = (
                after_quality["response_error_rate"]
                - before_quality["response_error_rate"]
            )
            socket_increase = (
                after_quality["socket_errors_per_second"]
                - before_quality["socket_errors_per_second"]
            )
            deltas["response_error_rate_increase"] = response_increase
            deltas["socket_errors_per_second_increase"] = socket_increase
            if response_increase > self.max_response_error_increase + 1e-12:
                reasons.append("response_errors_increased")
            if socket_increase > self.max_socket_error_increase + 1e-12:
                reasons.append("socket_errors_increased")
            raw_delta = self._percent_change(baseline_rps, candidate_rps)
            if raw_delta is not None:
                if not math.isfinite(raw_delta):
                    return CanaryVerdict(False, "rejected", ["invalid_metric_delta"], {})
                deltas["raw_requests_per_sec_percent"] = raw_delta
            baseline_rps = before_quality["successful_requests_per_sec"]
            candidate_rps = after_quality["successful_requests_per_sec"]

        p99_delta = self._percent_change(baseline_p99, candidate_p99)
        rps_delta = self._percent_change(baseline_rps, candidate_rps)
        if any(
            value is not None and not math.isfinite(value)
            for value in (p99_delta, rps_delta)
        ):
            return CanaryVerdict(False, "rejected", ["invalid_metric_delta"], {})
        if p99_delta is not None:
            deltas["p99_percent"] = p99_delta
        if rps_delta is not None:
            deltas["requests_per_sec_percent"] = rps_delta
            if quality_complete:
                deltas["successful_requests_per_sec_percent"] = rps_delta

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
            if p99_delta is not None and p99_delta > self.max_regression_percent * 2:
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
        if quality_required and not quality_complete:
            return CanaryVerdict(
                False, "inconclusive", ["missing_request_quality"], deltas
            )
        if quality_complete and before_quality["completed"] == 0:
            return CanaryVerdict(
                False, "inconclusive", ["no_baseline_requests"], deltas
            )
        if not objective_available:
            return CanaryVerdict(
                True,
                "inconclusive",
                ["missing_objective_metrics"],
                deltas,
            )
        return CanaryVerdict(True, "accepted", [], deltas)

    @staticmethod
    def _threshold(name: str, value: object, *, upper: float | None = None) -> float:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"{name} must be a finite non-negative number")
        try:
            parsed = float(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a finite non-negative number") from exc
        if (
            not math.isfinite(parsed)
            or parsed < 0
            or (upper is not None and parsed > upper)
        ):
            bound = f" and at most {upper}" if upper is not None else ""
            raise ValueError(f"{name} must be finite and non-negative{bound}")
        return parsed

    @staticmethod
    def _request_quality(values: Mapping[str, object]) -> dict:
        fields = (
            "requests_completed",
            "elapsed_seconds",
            "non_success_responses",
            "socket_errors",
        )
        result = {
            "known": any(key in values for key in (*fields, "request_quality_invalid")),
            "invalid": bool(values.get("request_quality_invalid")),
            "complete": False,
            "response_errors": None,
            "socket_errors": None,
        }
        for source, target in (
            ("requests_completed", "completed"),
            ("non_success_responses", "response_errors"),
            ("elapsed_seconds", "elapsed"),
        ):
            raw = values.get(source)
            if raw is None:
                continue
            parsed, invalid = CanaryVerifier._nonnegative_number(raw)
            if (
                invalid
                or parsed is None
                or (source != "elapsed_seconds" and not parsed.is_integer())
            ):
                result["invalid"] = True
            else:
                result[target] = parsed
        sockets = values.get("socket_errors")
        if sockets is not None:
            if not isinstance(sockets, Mapping) or set(sockets) != {
                "connect",
                "read",
                "write",
                "timeout",
            }:
                result["invalid"] = True
            else:
                counts = []
                for raw in sockets.values():
                    parsed, invalid = CanaryVerifier._nonnegative_number(raw)
                    if (
                        raw is None
                        or invalid
                        or parsed is None
                        or not parsed.is_integer()
                    ):
                        result["invalid"] = True
                    else:
                        counts.append(parsed)
                result["socket_errors"] = sum(counts)
        completed, elapsed = result.get("completed"), result.get("elapsed")
        errors = result["response_errors"]
        if elapsed is not None and elapsed <= 0:
            result["invalid"] = True
        if completed is not None and errors is not None and errors > completed:
            result["invalid"] = True
        if (
            all(
                value is not None
                for value in (completed, elapsed, errors, result["socket_errors"])
            )
            and not result["invalid"]
        ):
            result.update(
                complete=True,
                response_error_rate=errors / completed if completed else 0.0,
                socket_errors_per_second=result["socket_errors"] / elapsed,
                successful_requests_per_sec=(completed - errors) / elapsed,
            )
            if not all(
                math.isfinite(result[key])
                for key in (
                    "response_error_rate",
                    "socket_errors_per_second",
                    "successful_requests_per_sec",
                )
            ):
                result["invalid"] = True
                result["complete"] = False
        return result

    @staticmethod
    def _metric(values: Mapping[str, object], *keys: str) -> tuple[float | None, bool]:
        for key in keys:
            if key not in values:
                continue
            value = values[key]
            if value is None:
                continue
            if isinstance(value, bool):
                return None, True
            try:
                parsed = float(value)
            except (OverflowError, TypeError, ValueError):
                return None, True
            if not math.isfinite(parsed) or parsed < 0.0:
                return None, True
            return parsed, False
        return None, False

    @staticmethod
    def _nonnegative_number(value: object) -> tuple[float | None, bool]:
        if value is None:
            return 0.0, False
        if isinstance(value, bool):
            return None, True
        try:
            parsed = float(value)
        except (OverflowError, TypeError, ValueError):
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
