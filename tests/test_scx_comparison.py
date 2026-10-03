import subprocess
from pathlib import Path

from schedx.benchmark.scx_comparison import (
    ScxComparisonConfig,
    aggregate_rows,
    build_summary,
    parse_scheduler_list,
    run_scx_comparison,
    _start_scheduler_context,
)
from schedx.main import build_parser


def test_scheduler_list_adds_implicit_default_and_deduplicates():
    assert parse_scheduler_list("scx_simple,scx_agent,scx_simple") == (
        "default",
        "scx_simple",
        "scx_agent",
    )


def test_scheduler_list_is_idempotent_after_runner_parsing():
    parsed = parse_scheduler_list("scx_simple,scx_agent")
    assert parse_scheduler_list(parsed) == parsed


def test_scheduler_list_rejects_unallowlisted_name():
    try:
        parse_scheduler_list("bash")
    except ValueError as exc:
        assert "allowlisted" in str(exc)
    else:
        raise AssertionError("unallowlisted scheduler was accepted")


def test_scheduler_list_allows_confirmed_lifecycle_schedulers():
    assert parse_scheduler_list(
        "scx_simple,scx_qmap,scx_flatcg,scx_agent"
    ) == (
        "default",
        "scx_simple",
        "scx_qmap",
        "scx_flatcg",
        "scx_agent",
    )


def test_aggregate_rows_reports_mean_median_and_stdev():
    stats = aggregate_rows(
        [
            {"requests_per_sec": 10, "latency_avg_ms": 2, "p99_ms": 5, "background_cpu_ticks": 100},
            {"requests_per_sec": 20, "latency_avg_ms": 4, "p99_ms": 7, "background_cpu_ticks": 120},
            {"requests_per_sec": 30, "latency_avg_ms": 6, "p99_ms": 9, "background_cpu_ticks": 80},
        ]
    )
    assert stats["requests_per_sec"]["mean"] == 20.0
    assert stats["requests_per_sec"]["median"] == 20.0
    assert stats["requests_per_sec"]["stdev"] == 10.0


def test_summary_marks_missing_scheduler_skipped_and_does_not_fabricate_metrics(tmp_path: Path):
    config = ScxComparisonConfig(output=tmp_path, repeats=1)
    phases = {
        "default": {
            "status": "ok",
            "rows": [{"returncode": 0, "metrics": {"requests_per_sec": 100, "p99_ms": 10}, "requests_per_sec": 100, "p99_ms": 10}],
            "background_cpu_ticks": 100,
            "scheduler_stats": None,
        },
        "scx_simple": {
            "status": "skipped",
            "reason": "scheduler_not_found",
            "rows": [],
            "background_cpu_ticks": None,
            "scheduler_stats": None,
            "policy_capability": "unknown",
        },
    }
    summary = build_summary(config, {"cpu_count": 4}, phases, {"cgroup_clean": True})
    assert summary["schedulers"]["scx_simple"]["status"] == "skipped"
    assert summary["schedulers"]["scx_simple"]["statistics"]["requests_per_sec"]["mean"] is None
    assert not summary["schedulers"]["scx_simple"]["fairness"]["valid_for_claims"]


def test_cli_exposes_scx_compare_scheduler_option():
    args = build_parser().parse_args(
        [
            "benchmark",
            "scx-compare",
            "--schedulers",
            "scx_simple,scx_qmap,scx_flatcg,scx_agent",
        ]
    )
    assert args.name == "scx-compare"
    assert args.schedulers == "scx_simple,scx_qmap,scx_flatcg,scx_agent"


def test_lifecycle_scheduler_uses_no_shell_and_has_explicit_capability(
    monkeypatch, tmp_path: Path
):
    calls = []

    class Process:
        pid = 123

        @staticmethod
        def poll():
            return None

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Process()

    monkeypatch.setattr("schedx.benchmark.scx_comparison.subprocess.Popen", popen)
    monkeypatch.setattr("schedx.benchmark.scx_comparison.time.sleep", lambda _: None)
    context = _start_scheduler_context(
        "scx_qmap", "/usr/local/bin/scx_qmap", tmp_path / "qmap.log", []
    )
    context["output_handle"].close()

    assert context["mode"] == "lifecycle"
    assert context["policy_capability"] == "lifecycle_only"
    assert calls[0][0] == ["/usr/local/bin/scx_qmap"]
    assert "shell" not in calls[0][1]


def test_run_writes_timestamped_artifacts_and_skips_missing_schedulers(
    monkeypatch, tmp_path: Path
):
    wrk_output = """
    Latency   1.00ms  0.10ms  5.00ms
    99%   4.00ms
    Requests/sec: 1000.00
    """

    class Controller:
        def __init__(self, dry_run=False):
            self.dry_run = dry_run

        @staticmethod
        def state():
            return "disabled"

    def run(command, **kwargs):
        assert "shell" not in kwargs
        return subprocess.CompletedProcess(command, 0, wrk_output, "")

    ticks = iter((100, 200))
    monkeypatch.setattr(
        "schedx.benchmark.scx_comparison.collect_environment",
        lambda: {
            "wrk_available": True,
            "stress_ng_available": True,
            "sched_ext_available": True,
            "cpu_count": 4,
        },
    )
    monkeypatch.setattr(
        "schedx.benchmark.scx_comparison.shutil.which", lambda name: None
    )
    monkeypatch.setattr(
        "schedx.benchmark.scx_comparison.ScxController", Controller
    )
    monkeypatch.setattr(
        "schedx.benchmark.scx_comparison.start_stress", lambda *_: object()
    )
    monkeypatch.setattr(
        "schedx.benchmark.scx_comparison.stop_process", lambda *_: None
    )
    monkeypatch.setattr(
        "schedx.benchmark.scx_comparison.cleanup_stress", lambda: None
    )
    monkeypatch.setattr(
        "schedx.benchmark.scx_comparison.process_ids", lambda *_: [1]
    )
    monkeypatch.setattr(
        "schedx.benchmark.scx_comparison.cpu_ticks", lambda *_: next(ticks)
    )
    monkeypatch.setattr("schedx.benchmark.scx_comparison.subprocess.run", run)
    monkeypatch.setattr("schedx.benchmark.scx_comparison.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "schedx.benchmark.scx_comparison.time.strftime",
        lambda _: "2026-08-29_20-00-00",
    )
    monkeypatch.setattr(
        "schedx.benchmark.scx_comparison._cleanup",
        lambda: {
            "stress_stopped": True,
            "sched_ext_disabled": True,
            "cgroup_clean": True,
        },
    )

    result = run_scx_comparison(
        ScxComparisonConfig(
            schedulers=("scx_simple", "scx_agent"),
            repeats=1,
            warmup=0,
            output=tmp_path,
        )
    )
    run_dir = Path(result["run_dir"])

    assert (run_dir / "default_wrk_repeat1.txt").exists()
    assert (run_dir / "default_scheduler.log").exists()
    assert (run_dir / "scx_simple_scheduler.log").exists()
    assert (run_dir / "scx_agent_scheduler.log").exists()
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "summary.csv").exists()
    assert (run_dir / "report.md").exists()
    assert "capability" in (run_dir / "summary.csv").read_text(encoding="utf-8")
    assert "lifecycle_only" in (run_dir / "report.md").read_text(encoding="utf-8")
    summary = result["summary"]
    assert summary["schedulers"]["scx_simple"]["status"] == "skipped"
    assert summary["schedulers"]["scx_simple"]["policy_capability"] == "lifecycle_only"
    assert summary["schedulers"]["scx_agent"]["policy_capability"] == "task_policy_and_fairness"
