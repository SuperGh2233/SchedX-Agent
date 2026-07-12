from pathlib import Path

from schedx.benchmark.nginx import NginxBenchmarkConfig, build_cgroup_evidence, build_summary


def test_nginx_summary_means_and_improvement():
    rows = [
        {"phase": "baseline", "requests_per_sec": 100.0, "latency_avg_ms": 10.0, "p99_ms": 50.0},
        {"phase": "baseline", "requests_per_sec": 120.0, "latency_avg_ms": 12.0, "p99_ms": 60.0},
        {"phase": "interference", "requests_per_sec": 50.0, "latency_avg_ms": 20.0, "p99_ms": 100.0},
        {"phase": "schedx", "requests_per_sec": 75.0, "latency_avg_ms": 15.0, "p99_ms": 80.0},
    ]
    summary = build_summary(
        NginxBenchmarkConfig(output=Path("results")),
        {"cgroup_v2": True, "sched_ext_available": False, "mode": "cgroup-only fallback"},
        rows,
        {"schedx_base_removed": True},
    )
    assert summary["phases"]["baseline"]["mean_requests_per_sec"] == 110.0
    assert summary["improvement"]["rps_drop_due_to_interference_percent"] == 54.5455
    assert summary["improvement"]["rps_recovery_vs_interference_percent"] == 50.0
    assert summary["improvement"]["p99_reduction_vs_interference_percent"] == 20.0


def test_nginx_summary_divide_by_zero_protection():
    summary = build_summary(
        NginxBenchmarkConfig(output=Path("results")),
        {},
        [
            {"phase": "baseline", "requests_per_sec": 0.0, "latency_avg_ms": 0.0},
            {"phase": "interference", "requests_per_sec": 0.0, "latency_avg_ms": 0.0},
        ],
        {},
    )
    assert summary["improvement"]["rps_drop_due_to_interference_percent"] is None
    assert summary["improvement"]["latency_increase_due_to_interference_percent"] is None


def test_build_cgroup_evidence_matches_optimize_and_snapshot():
    optimize = {
        "returncode": 0,
        "data": {
            "results": [
                {
                    "pid": 123,
                    "status": "ok",
                    "group": "pid-123",
                    "action": {
                        "value": 50,
                        "metadata": {"comm": "stress-ng-cpu", "matched_by": "comm_exact"},
                    },
                }
            ]
        },
    }
    snapshot = {
        "groups": [
            {
                "path": "/sys/fs/cgroup/schedx/pid-123",
                "cpu_weight": "50",
                "cgroup_procs": ["123"],
            }
        ]
    }
    evidence = build_cgroup_evidence(optimize, snapshot)
    assert evidence["matched_action_count"] == 1
    assert evidence["groups"][0]["pid"] == "123"
    assert evidence["groups"][0]["cpu_weight"] == "50"
    assert evidence["groups"][0]["action_status"] == "ok"
