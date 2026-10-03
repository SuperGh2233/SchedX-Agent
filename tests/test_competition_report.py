import importlib.util
import json
from pathlib import Path


def load_report_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "generate_competition_report.py"
    )
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
    write_json(
        results / "competition-demo" / "cgroup-inheritance.json", {"passed": True}
    )
    write_json(
        results / "competition-demo" / "closed-loop-convergence.json", {"passed": True}
    )
    write_json(
        results / "multi-agent-llm" / "summary.json",
        {"passed": True, "successful_tools": 6, "agents": 6},
    )

    output = tmp_path / "reports" / "competition-final.md"
    module.generate(results, output)

    text = output.read_text(encoding="utf-8")
    assert "SchedX-Agent Competition Report" in text
    assert "84.96%" in text
    assert "deepseek-v4" in text
    assert "Four-Way Nginx Ablation" in text


def test_competition_report_reads_timestamped_demo_manifest(tmp_path: Path):
    module = load_report_module()
    results = tmp_path / "results"
    run_dir = results / "competition-demo" / "2026-07-18_12-00-00"
    write_json(
        run_dir / "manifest.json",
        {
            "status": "ok",
            "nginx_ablation": {
                "summary": {
                    "phases": {
                        "default": {
                            "mean_requests_per_sec": 100.0,
                            "mean_p99_ms": 10.0,
                        },
                        "cgroup_only": {"rps_gain_vs_default_percent": 10.0},
                        "scx_only": {"rps_gain_vs_default_percent": 20.0},
                        "agent_combined": {
                            "rps_gain_vs_default_percent": 30.0,
                            "valid_for_claims": True,
                        },
                    }
                }
            },
            "batch_throughput": {
                "summary": {
                    "phases": {
                        "baseline": {"mean_events_per_second": 1000.0},
                        "interference": {"mean_events_per_second": 600.0},
                        "schedx": {"throughput_gain_vs_interference_percent": 50.0},
                    }
                }
            },
            "accepted_canary": {
                "data": {
                    "agent_loop": {
                        "final_status": "success",
                        "context_data": {
                            "canary_verdict": {
                                "status": "accepted",
                                "deltas": {"requests_per_sec_percent": 12.0},
                            }
                        },
                    }
                }
            },
        },
    )

    output = tmp_path / "competition.md"
    module.generate(results, output, run_dir)
    text = output.read_text(encoding="utf-8")

    assert "30.00%" in text
    assert "50.00%" in text
    assert "`accepted`" in text
    assert "xychart-beta" in text


def test_native_report_prefers_fairness_valid_summary(tmp_path: Path):
    module = load_report_module()
    results = tmp_path / "results"
    write_json(
        results / "native-scx-old" / "summary.json",
        {
            "comparison": {
                "rps_gain_percent": 90.0,
                "background_cpu_retention_percent": 17.0,
                "valid_for_performance_claims": False,
            }
        },
    )
    write_json(
        results / "native-scx" / "fairness-valid" / "summary.json",
        {
            "comparison": {
                "rps_gain_percent": 62.18,
                "background_cpu_retention_percent": 32.22,
                "valid_for_performance_claims": True,
            }
        },
    )

    selected = module.latest_native_summary(results)

    assert selected["comparison"]["rps_gain_percent"] == 62.18


def test_competition_report_prefers_fair_batch_summary(tmp_path: Path):
    module = load_report_module()
    results = tmp_path / "results"
    run_dir = results / "competition-demo" / "2026-07-18_12-00-00"
    write_json(
        run_dir / "manifest.json",
        {
            "batch_throughput": {
                "summary": {
                    "phases": {
                        "schedx": {
                            "throughput_gain_vs_interference_percent": -1.0,
                            "valid_for_claims": False,
                        }
                    }
                }
            }
        },
    )
    write_json(
        results / "batch-throughput" / "formal" / "summary.json",
        {
            "phases": {
                "schedx": {
                    "throughput_gain_vs_interference_percent": 64.54,
                    "background_retention_percent": 27.19,
                    "valid_for_claims": True,
                }
            }
        },
    )

    data = module.extract(results, run_dir)

    assert data["batch"]["phases"]["schedx"]["valid_for_claims"]


def test_competition_report_includes_scx_comparison(tmp_path: Path):
    module = load_report_module()
    results = tmp_path / "results"
    write_json(
        results / "scx-compare-formal" / "run" / "summary.json",
        {
            "schedulers": {
                "scx_agent": {
                    "policy_capability": "task_policy_and_fairness",
                    "statistics": {
                        "requests_per_sec": {"mean": 114224.98},
                        "p99_ms": {"mean": 2.99},
                    },
                    "comparison_vs_default": {
                        "rps_gain_percent": 79.02,
                        "p99_reduction_percent": 60.04,
                    },
                    "background_retention_percent": 30.48,
                    "fairness": {"valid_for_claims": True},
                }
            }
        },
    )

    output = tmp_path / "competition.md"
    module.generate(results, output)
    text = output.read_text(encoding="utf-8")

    assert "Native sched_ext Scheduler Comparison" in text
    assert "79.02%" in text
    assert "task_policy_and_fairness" in text


def test_competition_report_includes_redis_confidence_intervals(tmp_path: Path):
    module = load_report_module()
    results = tmp_path / "results"
    write_json(
        results / "redis-formal-final" / "run" / "summary.json",
        {
            "phases": {
                "baseline": {
                    "requests_per_sec": {"mean": 120000, "ci95": [118000, 122000]},
                    "p99_ms": {"mean": 1.0, "ci95": [0.9, 1.1]},
                },
                "interference": {
                    "requests_per_sec": {"mean": 80000, "ci95": [76000, 84000]},
                    "p99_ms": {"mean": 3.1, "ci95": [3.0, 3.2]},
                },
                "schedx": {
                    "requests_per_sec": {"mean": 105000, "ci95": [102000, 108000]},
                    "p99_ms": {"mean": 1.9, "ci95": [1.8, 2.0]},
                    "background_retention_percent": 61.25,
                    "valid_for_claims": True,
                },
            },
            "schedx_qps_gain_vs_interference_percent": 31.03,
            "schedx_p99_reduction_vs_interference_percent": 38.75,
        },
    )

    output = tmp_path / "competition.md"
    module.generate(results, output)
    text = output.read_text(encoding="utf-8")

    assert "Redis Latency Scenario" in text
    assert "[102000.00, 108000.00]" in text
    assert "31.03%" in text
    assert "38.75%" in text
