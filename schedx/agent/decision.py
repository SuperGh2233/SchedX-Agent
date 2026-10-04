from __future__ import annotations

from dataclasses import dataclass, field
import math
import json
from collections.abc import Mapping
from typing import Any


@dataclass
class Decision:
    mode: str
    target: str
    parameters: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    confidence: float = 0.0


class DecisionEngine:
    """Autonomous decision engine for SchedX-Agent.

    Given workload classification and system pressure, selects the
    optimization mode and tunes parameters without user input.
    """

    MAX_STABILITY_WINDOW = 128

    def __init__(self, *, stability_window: int = 3, stability_tolerance_percent: float = 1.0) -> None:
        if isinstance(stability_window, bool) or not isinstance(stability_window, int) or not 2 <= stability_window <= self.MAX_STABILITY_WINDOW:
            raise ValueError("stability_window must be an integer in [2, 128]")
        tolerance = self._finite_number(stability_tolerance_percent)
        if tolerance is None or tolerance < 0:
            raise ValueError("stability_tolerance_percent must be finite and non-negative")
        self.stability_window = stability_window
        self.stability_tolerance_percent = tolerance

    def decide(self, classification: dict, pressure: dict, topology: dict | None = None, preferred_target: str = "") -> Decision:
        groups = classification.get("groups", {})
        overall = classification.get("overall", "unknown")
        psi = classification.get("psi_insight", {})

        latency_count = len(groups.get("latency_sensitive", []))
        batch_count = len(groups.get("batch_compute", []))
        noise_count = len(groups.get("background_noise", []))

        cpu_pressure = self._cpu_pressure_level(pressure)
        mem_pressure = self._mem_pressure_level(pressure)
        io_pressure = self._io_pressure_level(pressure)

        if overall == "mixed" and latency_count > 0 and noise_count > 0:
            return self._mixed_latency_noise(groups, cpu_pressure, mem_pressure, topology, preferred_target)
        if overall == "mixed" and latency_count > 0 and batch_count > 0:
            return self._mixed_latency_batch(groups, cpu_pressure, topology, preferred_target)
        if latency_count > 0 and batch_count == 0 and noise_count == 0:
            return Decision(
                mode="latency_first",
                target=preferred_target or self._pick_target(groups["latency_sensitive"]),
                parameters=self._latency_params(cpu_pressure),
                reason=f"pure latency workload ({latency_count} services), no interference",
                confidence=0.9,
            )
        if batch_count > 0 and latency_count == 0:
            return Decision(
                mode="throughput_first",
                target=self._pick_target(groups["batch_compute"]),
                parameters=self._throughput_params(cpu_pressure),
                reason=f"pure batch workload ({batch_count} jobs), optimizing throughput",
                confidence=0.85,
            )
        if noise_count > 0 and latency_count == 0 and batch_count == 0:
            return Decision(
                mode="isolate_background",
                target=self._pick_noise_target(groups.get("background_noise", [])),
                parameters=self._isolate_params(cpu_pressure),
                reason=f"only background noise ({noise_count} procs), isolating",
                confidence=0.7,
            )

        return Decision(
            mode="balanced",
            target=self._pick_any_target(groups),
            parameters={"cpu_weight": 500},
            reason="no clear workload pattern, using balanced mode",
            confidence=0.5,
        )

    def evaluate_result(self, before: dict, after: dict, mode: str) -> dict[str, Any]:
        """Evaluate optimization result and decide if re-optimization is needed."""
        improvement = self._calc_improvement(before, after, mode)
        needs_retry = improvement < 0
        suggested_adjustment = {}

        if needs_retry:
            if mode == "latency_first":
                suggested_adjustment = {
                    "cpu_weight_low": 25,
                    "nice_boost": -15,
                    "reason": "previous optimization insufficient, increasing isolation",
                }
            elif mode == "throughput_first":
                suggested_adjustment = {
                    "cpu_weight_high": 9500,
                    "reason": "throughput not improved, increasing priority",
                }

        return {
            "improvement": improvement,
            "needs_retry": needs_retry,
            "suggested_adjustment": suggested_adjustment,
            "verdict": "good" if improvement > 2 else ("marginal" if improvement >= 0 else "poor"),
        }

    def should_stop(self, history: list[dict]) -> bool:
        """Compatibility entry point; stability never stops continuous monitoring."""
        return self.assess_stability(history)["stable"]

    def assess_stability(self, history: list[dict]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "stable": False, "window": self.stability_window,
            "tolerance_percent": self.stability_tolerance_percent,
        }

        def reject(reason: str) -> dict[str, Any]:
            return {**result, "reason": reason}

        if len(history) < self.stability_window:
            return reject("insufficient_rounds")
        window = history[-self.stability_window:]
        if any(row.get("status") != "ok" or row.get("objective_status") != "accepted" for row in window):
            return reject("unmeasured_or_failed_round")
        decisions = [row.get("decision") for row in window]
        if any(not isinstance(d, Mapping) or any(not isinstance(d.get(key), str) or not d[key] for key in ("mode", "target")) for d in decisions):
            return reject("missing_objective_identity")
        identities = {(d["mode"], d["target"]) for d in decisions}
        if len(identities) != 1:
            return reject("objective_changed")
        try:
            policies = [json.dumps({"expert_id": d.get("expert_id"), "parameters": d.get("parameters", {})}, sort_keys=True, allow_nan=False) for d in decisions]
        except (TypeError, ValueError):
            return reject("invalid_policy_parameters")
        if len(set(policies)) != 1:
            return reject("policy_changed")
        objectives = [self.objective_metric(d["mode"], row.get("metrics")) for d, row in zip(decisions, window)]
        if any(value is None for value in objectives):
            return reject("missing_or_invalid_objective_metrics")
        if len({value[0] for value in objectives}) != 1:
            return reject("objective_metric_changed")
        values = [value[1] for value in objectives]
        average = sum(value / len(values) for value in values)
        variation = (max(values) - min(values)) / average * 100 if average else 0.0
        if not math.isfinite(variation):
            return reject("invalid_objective_variation")
        result.update(metric=objectives[0][0], values=values, variation_percent=round(variation, 6))
        improvements = [self._finite_number(row.get("improvement")) for row in window]
        if any(value is None for value in improvements):
            return reject("missing_or_invalid_improvement")
        result["maximum_step_change_percent"] = max(abs(value) for value in improvements)
        if variation > self.stability_tolerance_percent:
            return reject("objective_variation")
        if result["maximum_step_change_percent"] > self.stability_tolerance_percent:
            return reject("ongoing_objective_change")
        return {**result, "stable": True, "reason": "stable_measured_objective"}

    @classmethod
    def objective_metric(cls, mode: str, metrics: object) -> tuple[str, float] | None:
        if not isinstance(metrics, Mapping):
            return None
        latency = ("p99_ms", ("p99_ms", "p99_latency_ms", "mean_p99_ms"))
        throughput = ("requests_per_sec", ("successful_requests_per_sec", "requests_per_sec", "rps", "mean_requests_per_sec", "qps"))
        choices = [latency] if mode in {"latency_first", "isolate_background"} else [throughput]
        if mode == "balanced":
            choices.append(latency)
        for name, aliases in choices:
            for key in aliases:
                if metrics.get(key) is None:
                    continue
                value = cls._finite_number(metrics[key])
                return (name, value) if value is not None and value >= 0 else None
        return None

    @staticmethod
    def _finite_number(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    def adjust_parameters(self, base_params: dict, adjustment: dict) -> dict:
        params = dict(base_params)
        for key in ("cpu_weight", "cpu_weight_low", "cpu_weight_high", "nice_boost"):
            if key in adjustment:
                params[key] = adjustment[key]
        return params

    def _mixed_latency_noise(self, groups: dict, cpu_press: str, mem_press: str, topo: dict | None, preferred_target: str = "") -> Decision:
        target = preferred_target or self._pick_target(groups["latency_sensitive"])
        noise_target = self._pick_noise_target(groups.get("background_noise", []))
        params = self._latency_params(cpu_press)
        params["isolate_target"] = noise_target

        if cpu_press == "high":
            params["cpu_max_bg"] = "15000 100000"
            params["cpu_weight_bg"] = 10
            params["nice_bg"] = 15
            reason = f"high CPU pressure, aggressively isolating {noise_target} from {target}"
        else:
            params["cpu_max_bg"] = "10000 100000" if target == "nginx" else "25000 100000"
            params["cpu_weight_bg"] = 10 if target == "nginx" else 25
            params["nice_bg"] = 10
            reason = f"mixed workload: protecting {target}, isolating {noise_target}"

        return Decision(
            mode="latency_first",
            target=target,
            parameters=params,
            reason=reason,
            confidence=0.85,
        )

    def _mixed_latency_batch(self, groups: dict, cpu_press: str, topo: dict | None, preferred_target: str = "") -> Decision:
        target = preferred_target or self._pick_target(groups["latency_sensitive"])
        params = self._latency_params(cpu_press)
        params["batch_target"] = self._pick_target(groups["batch_compute"])

        return Decision(
            mode="latency_first",
            target=target,
            parameters=params,
            reason=f"mixed latency+batch: protecting {target}, batch gets remaining capacity",
            confidence=0.8,
        )

    def _latency_params(self, cpu_pressure: str) -> dict[str, Any]:
        if cpu_pressure == "high":
            return {"cpu_weight": 10000, "cpu_max_bg": "15000 100000", "nice_target": -15, "nice_bg": 15}
        if cpu_pressure == "medium":
            return {"cpu_weight": 10000, "cpu_max_bg": "25000 100000", "nice_target": -10, "nice_bg": 10}
        return {"cpu_weight": 8000, "cpu_max_bg": "50000 100000", "nice_target": -5, "nice_bg": 5}

    def _throughput_params(self, cpu_pressure: str) -> dict[str, Any]:
        if cpu_pressure == "high":
            return {
                "cpu_weight": 9500,
                "cpu_max": "max 100000",
                "cpu_weight_bg": 100,
                "cpu_max_bg": "50000 100000",
            }
        return {
            "cpu_weight": 9000,
            "cpu_max": "max 100000",
            "cpu_weight_bg": 300,
            "cpu_max_bg": "80000 100000",
        }

    def _isolate_params(self, cpu_pressure: str) -> dict[str, Any]:
        if cpu_pressure == "high":
            return {"cpu_weight_bg": 25, "cpu_max_bg": "10000 100000", "nice_bg": 19}
        return {"cpu_weight_bg": 50, "cpu_max_bg": "25000 100000", "nice_bg": 10}

    def _cpu_pressure_level(self, pressure: dict) -> str:
        some = pressure.get("cpu", {}).get("some", {})
        avg10 = some.get("avg10", 0.0)
        if avg10 > 20:
            return "high"
        if avg10 > 5:
            return "medium"
        return "low"

    def _mem_pressure_level(self, pressure: dict) -> str:
        some = pressure.get("memory", {}).get("some", {})
        avg10 = some.get("avg10", 0.0)
        if avg10 > 10:
            return "high"
        if avg10 > 3:
            return "medium"
        return "low"

    def _io_pressure_level(self, pressure: dict) -> str:
        some = pressure.get("io", {}).get("some", {})
        avg10 = some.get("avg10", 0.0)
        if avg10 > 20:
            return "high"
        if avg10 > 5:
            return "medium"
        return "low"

    def _pick_target(self, procs: list[dict]) -> str:
        if not procs:
            return ""
        best = max(procs, key=lambda p: float(p.get("cpu_percent", 0) or 0))
        return str(best.get("comm", ""))

    def _pick_noise_target(self, procs: list[dict]) -> str:
        if not procs:
            return ""
        best = max(procs, key=lambda p: float(p.get("cpu_percent", 0) or 0))
        return str(best.get("comm", ""))

    def _pick_any_target(self, groups: dict) -> str:
        for key in ("latency_sensitive", "batch_compute", "background_noise"):
            procs = groups.get(key, [])
            if procs:
                return self._pick_target(procs)
        return ""

    def _calc_improvement(self, before: dict, after: dict, mode: str) -> float:
        bm = before.get("metrics", {})
        am = after.get("metrics", {})
        if mode == "latency_first":
            bv = bm.get("p99_ms") or bm.get("p99_latency_ms") or bm.get("latency_avg_ms")
            av = am.get("p99_ms") or am.get("p99_latency_ms") or am.get("latency_avg_ms")
            if bv and av and bv > 0:
                return ((bv - av) / bv) * 100
        elif mode == "throughput_first":
            bv = bm.get("events_per_second") or bm.get("qps")
            av = am.get("events_per_second") or am.get("qps")
            if bv and av and bv > 0:
                return ((av - bv) / bv) * 100
        return 0.0
