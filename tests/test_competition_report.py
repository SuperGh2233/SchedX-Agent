import importlib.util
import json
from pathlib import Path


def load_report_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "generate_competition_report.py"
    spec = importlib.util.spec_from_file_location("generate_competition_report", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_competition_report_summarizes_current_artifacts(tmp_path: Path):
    module = load_report_module()
    results = tmp_path / "results"
    write_json(
        results / "native-scx-metrics-formal" / "summary.json",
        {"comparison": {"rps_gain_percent": 88.29, "p99_reduction_percent": 28.16}},
    )
    write_json(
        results / "llm-policy-comparison" / "summary.json",
        {
            "llm_source": "deepseek-v4",
            "llm_decision": {
                "mode": "latency_first",
                "target": "nginx",
                "reason": "test decision",
            },
            "comparison": {
                "llm": {"rps_gain_percent": 84.96, "p99_reduction_percent": 55.16},
                "rule": {"rps_gain_percent": 84.03, "p99_reduction_percent": 55.21},
            },
        },
    )
    write_json(
        results / "competition-demo" / "final-status.json",
        {
            "scheduler_running": True,
            "sched_ext": {"state": "enabled", "current_scheduler": "schedx_agent"},
        },
    )
    write_json(results / "competition-demo" / "cgroup-inheritance.json", {"passed": True})
    write_json(results / "competition-demo" / "closed-loop-convergence.json", {"passed": True})
    write_json(
        results / "multi-agent-llm" / "summary.json",
        {"passed": True, "successful_tools": 6, "agents": 6},
    )

    output = tmp_path / "reports" / "competition-final.md"
    module.generate(results, output)

    text = output.read_text(encoding="utf-8")
    assert "SchedX-Agent Competition Summary" in text
    assert "84.96%" in text
    assert "DeepSeek" in text
    assert "high" in text
