from schedx.policies.classifier import WorkloadClassifier
from schedx.policies.planner import PolicyPlanner, match_isolation_target


def test_classifier_detects_mixed_workload():
    snapshot = {
        "processes": [
            {"pid": 1, "comm": "nginx", "cpu_percent": 1.0},
            {"pid": 2, "comm": "stress-ng", "cpu_percent": 90.0},
        ]
    }
    result = WorkloadClassifier().classify_snapshot(snapshot)
    assert result["overall"] == "mixed"
    assert result["groups"]["background_noise"][0]["reason"]


def test_latency_policy_protects_target():
    classification = {"groups": {"background_noise": [{"pid": 2, "comm": "stress-ng"}]}}
    actions = PolicyPlanner().plan("latency_first", "nginx", classification)
    assert actions[0].target == "nginx"
    assert any(action.target == "2" for action in actions)


def test_classifier_reports_cpu_reason():
    proc = {"pid": 3, "comm": "worker", "cpu_percent": 80.0}
    result = WorkloadClassifier().classify_process_with_reason(proc)
    assert result["type"] == "batch_compute"
    assert "cpu_percent" in result["reason"]


def test_classifier_detects_nginx_latency():
    proc = {"pid": 10, "comm": "nginx", "cmdline": "nginx: worker process", "cpu_percent": 1.0}
    result = WorkloadClassifier().classify_process_with_reason(proc)
    assert result["type"] == "latency_sensitive"


def test_classifier_detects_redis_latency():
    proc = {"pid": 11, "comm": "redis-server", "cmdline": "redis-server 127.0.0.1:6379", "cpu_percent": 1.0}
    result = WorkloadClassifier().classify_process_with_reason(proc)
    assert result["type"] == "latency_sensitive"


def test_classifier_detects_stress_background():
    proc = {"pid": 12, "comm": "stress-ng", "cmdline": "stress-ng --cpu 4", "cpu_percent": 90.0}
    result = WorkloadClassifier().classify_process_with_reason(proc)
    assert result["type"] == "background_noise"


def test_classifier_keeps_plain_python_unknown():
    proc = {"pid": 13, "comm": "python3", "cmdline": "python3 app.py", "cpu_percent": 2.0}
    result = WorkloadClassifier().classify_process_with_reason(proc)
    assert result["type"] == "unknown"


def test_classifier_detects_python_benchmark_batch():
    proc = {"pid": 14, "comm": "python3", "cmdline": "python3 train_benchmark.py", "cpu_percent": 2.0}
    result = WorkloadClassifier().classify_process_with_reason(proc)
    assert result["type"] == "batch_compute"


def test_isolate_background_allows_stress_ng_cpu():
    classification = {
        "groups": {
            "background_noise": [
                {"pid": 20, "comm": "stress-ng-cpu", "cmdline": "stress-ng --cpu 4"}
            ]
        }
    }
    actions = PolicyPlanner().plan("isolate_background", "stress-ng", classification)
    assert len(actions) == 2
    assert actions[0].target == "20"
    assert actions[0].metadata["matched_by"] == "comm_exact"
    assert actions[1].action == "set_cgroup_cpu_max"


def test_isolate_background_allows_stress_ng_parent():
    classification = {
        "groups": {
            "background_noise": [
                {"pid": 21, "comm": "stress-ng", "cmdline": "stress-ng --cpu 4 --timeout 300s"}
            ]
        }
    }
    actions = PolicyPlanner().plan("isolate_background", "stress-ng", classification)
    assert len(actions) == 2
    assert actions[0].target == "21"
    assert actions[0].metadata["matched_by"] == "comm_exact"
    assert actions[1].action == "set_cgroup_cpu_max"


def test_isolate_background_skips_schedx_control_process():
    classification = {
        "groups": {
            "background_noise": [
                {"pid": 22, "comm": "schedx", "cmdline": "python -m schedx.main classify", "cpu_percent": 99.0}
            ]
        }
    }
    actions = PolicyPlanner().plan("isolate_background", "stress-ng", classification)
    assert actions == []


def test_isolate_background_skips_system_control_processes():
    protected = ("sshd", "bash", "systemd", "NetworkManager", "firewalld", "tuned")
    classification = {
        "groups": {
            "background_noise": [
                {"pid": index, "comm": comm, "cmdline": comm, "cpu_percent": 99.0}
                for index, comm in enumerate(protected, start=30)
            ]
        }
    }
    actions = PolicyPlanner().plan("isolate_background", "stress-ng", classification)
    assert actions == []


def test_stress_ng_cmdline_match_records_matcher():
    proc = {"pid": 40, "comm": "worker", "cmdline": "/usr/bin/stress-ng --cpu 4"}
    match = match_isolation_target(proc, "stress-ng")
    assert match["matched"]
    assert match["matched_by"] == "cmdline_contains"


def test_latency_policy_adds_hard_cpu_isolation_on_four_cpus():
    classification = {
        "groups": {
            "background_noise": [{"pid": 2, "comm": "stress-ng"}],
        }
    }
    topology = {
        "total_cpus": 4,
        "performance_mask": "0,1",
        "efficiency_mask": "2,3",
    }

    actions = PolicyPlanner().plan("latency_first", "nginx", classification, topology)

    assert not any(a.action == "set_cpuset_cpus" and a.target == "nginx" for a in actions)
    assert any(a.action == "set_cpuset_cpus" and a.target == "2" and a.value == "2,3" for a in actions)


def test_isolation_policy_skips_cpuset_on_small_system():
    classification = {"groups": {"background_noise": [{"pid": 2, "comm": "stress-ng"}]}}
    topology = {"total_cpus": 2, "performance_mask": "0", "efficiency_mask": "1"}

    actions = PolicyPlanner().plan("isolate_background", "stress-ng", classification, topology)

    assert not any(a.action == "set_cpuset_cpus" for a in actions)


def test_policy_uses_agent_selected_background_quota():
    classification = {"groups": {"background_noise": [{"pid": 2, "comm": "stress-ng"}]}}

    actions = PolicyPlanner().plan(
        "latency_first",
        "nginx",
        classification,
        parameters={"cpu_weight": 8000, "cpu_weight_bg": 25, "cpu_max_bg": "15000 100000"},
    )

    assert any(a.action == "set_cgroup_cpu_weight" and a.target == "nginx" and a.value == 8000 for a in actions)
    assert any(a.action == "set_cgroup_cpu_weight" and a.target == "2" and a.value == 25 for a in actions)
    assert any(a.action == "set_cgroup_cpu_max" and a.target == "2" and a.value == "15000 100000" for a in actions)
