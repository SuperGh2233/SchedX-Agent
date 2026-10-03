from pathlib import Path

from schedx.benchmark.common import describe
from schedx.benchmark.redis import (
    RedisBenchmarkConfig,
    RedisMixedBenchmark,
    build_redis_summary,
)
from schedx.benchmark.redis_parser import parse_redis_benchmark_output


REDIS_OUTPUT = """
====== GET ======
  10000 requests completed in 0.25 seconds
Latency by percentile distribution:
50.000% <= 0.191 milliseconds (cumulative count 5369)
Summary:
  throughput summary: 40000.00 requests per second
  latency summary (msec):
          avg       min       p50       p95       p99       max
        0.195     0.040     0.191     0.279     0.359     0.591
"""


def test_parse_detailed_redis_benchmark_output():
    metrics = parse_redis_benchmark_output(REDIS_OUTPUT)
    assert metrics["requests_per_sec"] == 40000.0
    assert metrics["requests"] == 10000.0
    assert metrics["elapsed_seconds"] == 0.25
    assert metrics["latency_avg_ms"] == 0.195
    assert metrics["p50_ms"] == 0.191
    assert metrics["p95_ms"] == 0.279
    assert metrics["p99_ms"] == 0.359
    assert metrics["latency_max_ms"] == 0.591


def test_describe_uses_small_sample_confidence_interval():
    stats = describe([{"x": 10}, {"x": 12}, {"x": 14}], "x")
    assert stats["count"] == 3
    assert stats["mean"] == 12.0
    assert stats["median"] == 12.0
    assert stats["stdev"] == 2.0
    assert stats["ci95"] == [7.0313, 16.9687]


def test_redis_summary_enforces_interference_and_fairness_floor(tmp_path: Path):
    rows = [
        {"phase": "baseline", "requests_per_sec": 1000, "p99_ms": 1, "wall_seconds": 1, "background_cpu_ticks": 0},
        {"phase": "baseline", "requests_per_sec": 900, "p99_ms": 1, "wall_seconds": 1, "background_cpu_ticks": 0},
        {"phase": "interference", "requests_per_sec": 500, "p99_ms": 4, "wall_seconds": 2, "background_cpu_ticks": 100},
        {"phase": "interference", "requests_per_sec": 600, "p99_ms": 5, "wall_seconds": 2, "background_cpu_ticks": 100},
        {"phase": "schedx", "requests_per_sec": 800, "p99_ms": 2, "wall_seconds": 1.5, "background_cpu_ticks": 40},
        {"phase": "schedx", "requests_per_sec": 900, "p99_ms": 2.5, "wall_seconds": 1.5, "background_cpu_ticks": 40},
    ]
    summary = build_redis_summary(
        RedisBenchmarkConfig(output=tmp_path),
        {"redis_status": "PONG"},
        rows,
        {phase: [] for phase in RedisMixedBenchmark.phases},
        [],
    )
    assert summary["interference_qps_drop_percent"] == 42.1053
    assert summary["schedx_qps_gain_vs_interference_percent"] == 54.5455
    assert summary["schedx_p99_reduction_vs_interference_percent"] == 50.0
    assert summary["phases"]["schedx"]["background_retention_percent"] == 40.0
    assert summary["phases"]["schedx"]["valid_for_claims"] is True


def test_redis_run_rotates_phase_order(monkeypatch, tmp_path: Path):
    calls: list[tuple[int, str]] = []

    monkeypatch.setattr(
        "schedx.benchmark.redis.collect_environment", lambda: {"kernel": "test"}
    )
    monkeypatch.setattr(
        "schedx.benchmark.redis.shutil.which", lambda tool: f"/usr/bin/{tool}"
    )
    monkeypatch.setattr(
        "schedx.benchmark.redis.RedisMixedBenchmark._redis_ping", lambda *_: "PONG"
    )
    monkeypatch.setattr(
        "schedx.benchmark.redis.RedisMixedBenchmark._cleanup", lambda *_: None
    )
    monkeypatch.setattr(
        "schedx.benchmark.redis.ScxController.state", lambda *_: "disabled"
    )
    monkeypatch.setattr(
        "schedx.benchmark.redis.time.strftime", lambda *_: "2026-08-30_00-00-00"
    )

    def run_repeat(self, phase, repeat, config, run_dir):
        calls.append((repeat, phase))
        return (
            {
                "phase": phase,
                "repeat": repeat,
                "requests_per_sec": 100.0,
                "p99_ms": 1.0,
                "wall_seconds": 1.0,
                "background_cpu_ticks": 10 if phase != "baseline" else 0,
                "notes": "",
            },
            {"status": "ok"},
        )

    monkeypatch.setattr(RedisMixedBenchmark, "_run_repeat", run_repeat)
    result = RedisMixedBenchmark().run(
        RedisBenchmarkConfig(repeats=3, output=tmp_path)
    )
    assert result["status"] == "ok"
    assert calls == [
        (1, "baseline"),
        (1, "interference"),
        (1, "schedx"),
        (2, "interference"),
        (2, "schedx"),
        (2, "baseline"),
        (3, "schedx"),
        (3, "baseline"),
        (3, "interference"),
    ]
    assert (tmp_path / "2026-08-30_00-00-00" / "report.md").exists()
