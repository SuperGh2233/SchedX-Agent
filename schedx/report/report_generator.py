from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ReportGenerator:
    PAIRS = {
        "nginx": ("nginx_default.json", "nginx_schedx.json", "p99_latency_ms", "lower"),
        "redis": ("redis_default.json", "redis_schedx.json", "qps", "higher"),
        "batch": ("batch_default.json", "batch_schedx.json", "elapsed_seconds", "lower"),
    }

    def generate(self, results_dir: Path = Path("results"), output: Path = Path("reports/report.md")) -> Path:
        output.parent.mkdir(parents=True, exist_ok=True)
        rows = self._comparison_rows(results_dir)
        self._try_generate_figures(rows, output.parent / "figures")

        lines = [
            "# SchedX-Agent Experiment Report",
            "",
            "## Summary",
            "",
            "This report is generated from JSON files in `results/`.",
            "",
            "## Before/After Comparison",
            "",
            "| Scenario | Metric | Default | SchedX | Improvement | Status |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
        if rows:
            for row in rows:
                lines.append(
                    f"| {row['scenario']} | {row['metric']} | {row['default']} | "
                    f"{row['schedx']} | {row['improvement']} | {row['status']} |"
                )
        else:
            lines.append("| no complete pair | n/a | n/a | n/a | n/a | missing data |")

        lines.extend(["", "## Raw Results", ""])
        for path in sorted(results_dir.glob("*.json")) if results_dir.exists() else []:
            data = _read_json(path)
            if data is None:
                continue
            lines.append(f"### {path.name}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(data, indent=2))
            lines.append("```")
            lines.append("")
        output.write_text("\n".join(lines), encoding="utf-8")
        return output

    def _comparison_rows(self, results_dir: Path) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for scenario, (default_name, schedx_name, metric, direction) in self.PAIRS.items():
            default = _read_json(results_dir / default_name)
            schedx = _read_json(results_dir / schedx_name)
            if not default or not schedx:
                continue
            default_value = _metric(default, metric)
            schedx_value = _metric(schedx, metric)
            if default_value is None or schedx_value is None:
                rows.append(
                    {
                        "scenario": scenario,
                        "metric": metric,
                        "default": "n/a",
                        "schedx": "n/a",
                        "improvement": "n/a",
                        "status": "metric missing",
                    }
                )
                continue
            rows.append(
                {
                    "scenario": scenario,
                    "metric": metric,
                    "default": f"{default_value:.3f}",
                    "schedx": f"{schedx_value:.3f}",
                    "improvement": _improvement(default_value, schedx_value, direction),
                    "status": "ok",
                }
            )
        return rows

    def _try_generate_figures(self, rows: list[dict[str, str]], figures_dir: Path) -> None:
        try:
            import matplotlib.pyplot as plt  # type: ignore
        except Exception:
            return
        if not rows:
            return
        figures_dir.mkdir(parents=True, exist_ok=True)
        for row in rows:
            if row["default"] == "n/a" or row["schedx"] == "n/a":
                continue
            scenario = row["scenario"]
            metric = row["metric"]
            values = [float(row["default"]), float(row["schedx"])]
            plt.figure(figsize=(4, 3))
            plt.bar(["default", "schedx"], values, color=["#65758b", "#1f9d7a"])
            plt.title(f"{scenario} {metric}")
            plt.tight_layout()
            file_name = {
                "nginx": "nginx_latency.png",
                "redis": "redis_qps.png",
                "batch": "batch_time.png",
            }.get(scenario, f"{scenario}.png")
            plt.savefig(figures_dir / file_name)
            plt.close()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _metric(data: dict[str, Any], name: str) -> float | None:
    metrics = data.get("metrics") or {}
    value = metrics.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _improvement(default: float, schedx: float, direction: str) -> str:
    if default == 0:
        return "n/a"
    if direction == "lower":
        value = ((default - schedx) / default) * 100.0
    else:
        value = ((schedx - default) / default) * 100.0
    return f"{value:.2f}%"
