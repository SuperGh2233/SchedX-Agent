from scripts.verify_tool_admission import assess, summarize, workload_command


def run(mode, repeat, foreground=1.0, throughput=10, progress=1):
    return {"mode": mode, "repeat": repeat, "failures": [], "foreground_p99_seconds": foreground,
            "background_p99_seconds": 2, "successful_jobs_per_second": throughput,
            "background_cpu_seconds_per_wall_second": progress}


def test_five_pairs_with_neutral_metrics_pass_regression_gate():
    rows = [run(mode, repeat) for repeat in range(5) for mode in ("off", "fixed", "adaptive")]
    result = assess(rows)
    assert result["status"] == "passed"
    assert result["comparisons"]["adaptive"]["foreground_p99_seconds"]["valid_pairs"] == 5


def test_lost_background_progress_and_stable_foreground_regression_are_not_hidden():
    rows = [run(mode, repeat, foreground=1.1 if mode == "adaptive" else 1,
                progress=.2 if mode == "fixed" else 1) for repeat in range(5) for mode in ("off", "fixed", "adaptive")]
    result = assess(rows)
    assert result["status"] == "not_accepted"
    assert "adaptive:foreground_p99_seconds:stable_regression_over_5_percent" in result["failures"]
    assert result["comparisons"]["fixed"]["foreground_p99_seconds"]["valid_pairs"] == 0


def test_failed_tools_and_missing_cpu_evidence_invalidate_the_burst():
    summary = summarize([{"index": 0, "intent": "interactive", "returncode": 124, "command_started": False},
                         {"index": 1, "intent": "compile", "returncode": 0, "command_started": True}], 1, 2)
    assert summary["status"] == "failed"
    assert "missing_background_cpu_evidence" in summary["failures"]
    assert any("unsuccessful_tool" in reason for reason in summary["failures"])


def test_workload_program_has_real_loop_and_records_cpu_affinity():
    command = workload_command({0, 2}, .05)
    compile(command[2], "workload", "exec")
    assert "os.sched_setaffinity(0, [0, 2])" in command[2]
    assert "while time.process_time() < end:" in command[2]
