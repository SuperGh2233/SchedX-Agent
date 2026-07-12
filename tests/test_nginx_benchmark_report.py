from pathlib import Path

from schedx.benchmark.nginx import NginxBenchmarkConfig, _write_report


def test_nginx_benchmark_report_contains_sections(tmp_path: Path):
    summary = {
        "phases": {
            "baseline": {"mean_requests_per_sec": 100.0, "mean_latency_avg_ms": 10.0, "mean_p99_ms": 50.0},
            "interference": {"mean_requests_per_sec": 50.0, "mean_latency_avg_ms": 20.0, "mean_p99_ms": 100.0},
            "schedx": {"mean_requests_per_sec": 75.0, "mean_latency_avg_ms": 15.0, "mean_p99_ms": 80.0},
        },
        "improvement": {},
        "cleanup": {"schedx_base_removed": True},
        "cgroup_evidence": {
            "optimize_returncode": 0,
            "matched_action_count": 1,
            "groups": [
                {
                    "pid": "123",
                    "comm": "stress-ng-cpu",
                    "matched_by": "comm_exact",
                    "path": "/sys/fs/cgroup/schedx/pid-123",
                    "cpu_weight": "50",
                    "cgroup_procs": ["123"],
                    "action_status": "ok",
                }
            ],
        },
    }
    _write_report(
        tmp_path,
        NginxBenchmarkConfig(output=tmp_path),
        {"os_release": "openEuler", "uname": "kernel", "cgroup_v2": True, "mode": "cgroup-only fallback"},
        [],
        summary,
    )
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Environment" in text
    assert "Baseline" in text
    assert "Interference" in text
    assert "SchedX" in text
    assert "Cgroup Evidence" in text
    assert "stress-ng-cpu" in text
    assert "/sys/fs/cgroup/schedx/pid-123" in text
    assert "| 123 |" in text
