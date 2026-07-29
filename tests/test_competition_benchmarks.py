from pathlib import Path

from schedx.benchmark.ablation import NginxAblationConfig, build_ablation_summary
from schedx.benchmark.batch import BatchThroughputConfig, build_batch_summary
from schedx.main import build_parser


def test_ablation_summary_isolates_each_execution_plane(tmp_path: Path):
    config = NginxAblationConfig(
        output=tmp_path, minimum_background_retention_percent=25.0
    )
    phases = {
        "default": {
            "rows": [{"requests_per_sec": 100.0, "p99_ms": 10.0}],
            "background_cpu_ticks": 100,
        },
        "cgroup_only": {
            "rows": [{"requests_per_sec": 120.0, "p99_ms": 8.0}],
            "background_cpu_ticks": 30,
        },
        "scx_only": {
            "rows": [{"requests_per_sec": 140.0, "p99_ms": 7.0}],
            "background_cpu_ticks": 40,
        },
        "agent_combined": {
            "rows": [{"requests_per_sec": 150.0, "p99_ms": 6.0}],
            "background_cpu_ticks": 20,
        },
    }

    summary = build_ablation_summary(config, {}, phases)

    assert summary["phases"]["cgroup_only"]["rps_gain_vs_default_percent"] == 20.0
    assert summary["phases"]["scx_only"]["p99_reduction_vs_default_percent"] == 30.0
    assert summary["phases"]["agent_combined"]["background_retention_percent"] == 20.0
    assert not summary["phases"]["agent_combined"]["valid_for_claims"]


def test_batch_summary_reports_interference_and_schedx_recovery(tmp_path: Path):
    config = BatchThroughputConfig(output=tmp_path)
    rows = [
        {
            "phase": "baseline",
            "events_per_second": 1000.0,
            "latency_p95_ms": 2.0,
            "background_cpu_ticks": 0,
        },
        {
            "phase": "interference",
            "events_per_second": 600.0,
            "latency_p95_ms": 5.0,
            "background_cpu_ticks": 100,
        },
        {
            "phase": "schedx",
            "events_per_second": 900.0,
            "latency_p95_ms": 3.0,
            "background_cpu_ticks": 50,
        },
    ]

    summary = build_batch_summary(
        config,
        {},
        rows,
        {phase: [] for phase in ("baseline", "interference", "schedx")},
    )

    assert (
        summary["phases"]["interference"]["throughput_drop_vs_baseline_percent"] == 40.0
    )
    assert (
        summary["phases"]["schedx"]["throughput_gain_vs_interference_percent"] == 50.0
    )
    assert summary["phases"]["schedx"]["background_retention_percent"] == 50.0
    assert summary["phases"]["schedx"]["valid_for_claims"]
    assert summary["interference_detected"]


def test_batch_summary_marks_short_run_without_real_interference(tmp_path: Path):
    rows = [
        {
            "phase": "baseline",
            "events_per_second": 1000.0,
            "background_cpu_ticks": 0,
        },
        {
            "phase": "interference",
            "events_per_second": 1010.0,
            "background_cpu_ticks": 100,
        },
        {
            "phase": "schedx",
            "events_per_second": 1005.0,
            "background_cpu_ticks": 90,
        },
    ]

    summary = build_batch_summary(
        BatchThroughputConfig(output=tmp_path),
        {},
        rows,
        {phase: [] for phase in ("baseline", "interference", "schedx")},
    )

    assert not summary["interference_detected"]


def test_cli_exposes_competition_benchmark_commands():
    parser = build_parser()
    ablation = parser.parse_args(
        [
            "benchmark",
            "nginx-ablation",
            "--duration",
            "5",
            "--repeats",
            "1",
            "--min-background-retention",
            "30",
        ]
    )
    batch = parser.parse_args(["benchmark", "batch-throughput", "--threads", "4"])

    assert ablation.name == "nginx-ablation"
    assert ablation.min_background_retention == 30.0
    assert batch.name == "batch-throughput"
