from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any


METRICS = {
    "nginx": {"p99_ms": "lower", "requests_per_sec": "higher"},
    "redis": {"qps": "higher", "p50_latency_ms": "lower"},
    "batch": {"events_per_second": "higher"},
}


class RepeatSummaryGenerator:
    def generate(self, results_dir: Path, output: Path) -> dict[str, Any]:
        summary = self.summarize(results_dir)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(self._markdown(summary), encoding="utf-8")
        output.with_suffix(".json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary

    def summarize(self, results_dir: Path) -> dict[str, Any]:
        repeats = sorted(path for path in results_dir.iterdir() if path.is_dir())
        scenarios: dict[str, Any] = {}
        for scenario, metrics in METRICS.items():
            scenario_metrics: dict[str, Any] = {}
            for metric, direction in metrics.items():
                samples = self._samples(repeats, scenario, metric, direction)
                if samples:
                    improvements = [sample["improvement_percent"] for sample in samples]
                    scenario_metrics[metric] = {
                        "direction": direction,
                        "samples": samples,
                        "mean_improvement_percent": round(statistics.mean(improvements), 2),
                        "stdev_improvement_percent": round(statistics.stdev(improvements), 2)
                        if len(improvements) > 1
                        else 0.0,
                    }
            if scenario_metrics:
                scenarios[scenario] = scenario_metrics
        return {"repeat_count": len(repeats), "scenarios": scenarios}

    def _samples(
        self, repeats: list[Path], scenario: str, metric: str, direction: str
    ) -> list[dict[str, float | str]]:
        samples: list[dict[str, float | str]] = []
        for repeat in repeats:
            default = _metric(repeat / f"{scenario}_default.json", metric)
            schedx = _metric(repeat / f"{scenario}_schedx.json", metric)
            if default is None or schedx is None or default == 0:
                continue
            improvement = (
                ((default - schedx) / default) * 100
                if direction == "lower"
                else ((schedx - default) / default) * 100
            )
            samples.append(
                {
                    "repeat": repeat.name,
                    "default": default,
                    "schedx": schedx,
                    "improvement_percent": round(improvement, 2),
                }
            )
        return samples

    def _markdown(self, summary: dict[str, Any]) -> str:
        lines = [
            "# SchedX-Agent Repeated Experiment Summary",
            "",
            f"Repeat directories found: {summary['repeat_count']}",
            "",
            "| Scenario | Metric | Mean Improvement | Stddev | Samples |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
        for scenario, metrics in summary["scenarios"].items():
            for metric, data in metrics.items():
                lines.append(
                    f"| {scenario} | {metric} | {data['mean_improvement_percent']:.2f}% | "
                    f"{data['stdev_improvement_percent']:.2f}% | {len(data['samples'])} |"
                )
        return "\n".join(lines) + "\n"


def _metric(path: Path, metric: str) -> float | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("metrics", {}).get(metric))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
