from __future__ import annotations

from dataclasses import dataclass, field
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
        """Decide if further optimization rounds should stop."""
        if len(history) >= 3:
            return True
        if len(history) >= 2:
            last_two = history[-2:]
            if all(h.get("improvement", 0) < 0 for h in last_two):
                return True
            if all(h.get("improvement", 0) > 5 for h in last_two):
                return True
        return False

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
