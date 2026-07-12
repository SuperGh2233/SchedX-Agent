from schedx.benchmark.runner import (
    parse_redis_benchmark_output,
    parse_sysbench_output,
    parse_wrk_output,
)
from schedx.benchmark.nginx import NginxBenchmarkConfig, NginxStressBenchmark


def test_parse_wrk_output():
    output = """
    Latency   12.34ms    5.00ms  80.00ms   90.00%
    Req/Sec   10.00k     1.00k   20.00k    80.00%
    50%   10.00ms
    75%   20.00ms
    90%   40.00ms
    99%   75.00ms
    Requests/sec: 12345.67
    Transfer/sec: 1.23MB
    """
    metrics = parse_wrk_output(output)
    assert metrics["qps"] == 12345.67
    assert metrics["requests_per_sec"] == 12345.67
    assert metrics["avg_latency_ms"] == 12.34
    assert metrics["latency_max_ms"] == 80.0
    assert metrics["p50_ms"] == 10.0
    assert metrics["p75_ms"] == 20.0
    assert metrics["p90_ms"] == 40.0
    assert metrics["p99_latency_ms"] == 75.0
    assert metrics["transfer_sec"] == "1.23MB"


def test_parse_redis_benchmark_output():
    output = "GET: 100000.00 requests per second, p50=0.191 msec p95=0.503 msec p99=0.815 msec"
    metrics = parse_redis_benchmark_output(output)
    assert metrics["qps"] == 100000.0
    assert metrics["p99_latency_ms"] == 0.815


def test_parse_sysbench_output():
    output = """
    total number of events:              1234
    total time:                          10.0023s
    events per second:                   123.37
    """
    metrics = parse_sysbench_output(output)
    assert metrics["elapsed_seconds"] == 10.0023
    assert metrics["events"] == 1234.0
    assert metrics["events_per_second"] == 123.37


def test_nginx_benchmark_reports_missing_wrk(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "schedx.benchmark.nginx.collect_environment",
        lambda: {"wrk_available": False, "stress_ng_available": True, "mode": "cgroup-only fallback"},
    )
    result = NginxStressBenchmark().run(NginxBenchmarkConfig(output=tmp_path))
    assert result["status"] == "failed"
    assert "wrk not found" in result["error"]
