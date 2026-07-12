import json
from pathlib import Path

from schedx.report.report_generator import ReportGenerator


def test_report_generates_comparison(tmp_path: Path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "nginx_default.json").write_text(
        json.dumps({"metrics": {"p99_latency_ms": 100.0}}),
        encoding="utf-8",
    )
    (results / "nginx_schedx.json").write_text(
        json.dumps({"metrics": {"p99_latency_ms": 80.0}}),
        encoding="utf-8",
    )
    output = tmp_path / "reports" / "report.md"
    ReportGenerator().generate(results, output)
    text = output.read_text(encoding="utf-8")
    assert "Before/After Comparison" in text
    assert "20.00%" in text

