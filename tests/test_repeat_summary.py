import json
from pathlib import Path

from schedx.report.repeat_summary import RepeatSummaryGenerator


def test_repeat_summary_calculates_mean_and_stdev(tmp_path: Path):
    for index, schedx_value in enumerate((80.0, 70.0), start=1):
        repeat = tmp_path / f"repeat-{index}"
        repeat.mkdir()
        (repeat / "nginx_default.json").write_text(
            json.dumps({"metrics": {"p99_ms": 100.0}}), encoding="utf-8"
        )
        (repeat / "nginx_schedx.json").write_text(
            json.dumps({"metrics": {"p99_ms": schedx_value}}), encoding="utf-8"
        )

    summary = RepeatSummaryGenerator().summarize(tmp_path)

    metric = summary["scenarios"]["nginx"]["p99_ms"]
    assert metric["mean_improvement_percent"] == 25.0
    assert metric["stdev_improvement_percent"] == 7.07
