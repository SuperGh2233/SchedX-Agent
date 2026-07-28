import threading
import os
from pathlib import Path

import pytest

from schedx.scx_daemon import (
    ScxDaemonClient,
    _ScxDaemonServer,
    _cpu_pressure_avg10,
    _pid_exists,
    _runtime_shares,
    choose_background_interval,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix socket daemon is Linux-only")


class FakeProcess:
    def poll(self):
        return None


class FakeStats:
    def to_dict(self):
        return {"total": 7}


class FakeController:
    def __init__(self):
        self._process = FakeProcess()
        self.policies = {}
        self.cgroup_policies = {}
        self.cgroup_metrics = {}
        self.removed_metric_ids = []
        self.fairness = None

    def status(self):
        return {"state": "enabled"}

    def set_task_policy(self, pid, class_id, weight):
        self.policies[pid] = (class_id, weight)
        return True

    def remove_task_policy(self, pid):
        self.policies.pop(pid, None)
        return True

    def set_cgroup_policy(self, cgroup_id, class_id, weight):
        self.cgroup_policies[cgroup_id] = (class_id, weight)
        return True

    def remove_cgroup_policy(self, cgroup_id):
        self.cgroup_policies.pop(cgroup_id, None)
        self.cgroup_metrics.pop(cgroup_id, None)
        return True

    def remove_cgroup_metrics(self, cgroup_id):
        self.removed_metric_ids.append(cgroup_id)
        self.cgroup_metrics.pop(cgroup_id, None)
        return True

    def set_fairness(self, background_interval, default_interval):
        self.fairness = (background_interval, default_interval)
        return True

    def get_stats(self):
        return FakeStats()

    def get_cgroup_metrics(self):
        return self.cgroup_metrics

    def dump_policies(self):
        return {"task_policies": self.policies, "cgroup_policies": self.cgroup_policies}


def test_daemon_supports_shared_policy_updates(tmp_path: Path):
    socket_path = tmp_path / "scx.sock"
    controller = FakeController()
    server = _ScxDaemonServer(socket_path, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        first = ScxDaemonClient(socket_path)
        second = ScxDaemonClient(socket_path)
        assert first.is_available()
        assert first.set_task_policy(101, 1, 5000)
        assert second.set_task_policy(202, 2, 1500)
        assert controller.policies == {101: (1, 5000), 202: (2, 1500)}
        assert first.remove_task_policy(101)
        assert controller.policies == {202: (2, 1500)}
        assert second.request("stats")["stats"]["total"] == 7
        assert first.set_cgroup_policy(303, 3, 250)
        assert controller.cgroup_policies == {303: (3, 250)}
        assert second.remove_cgroup_policy(303)
    finally:
        server.shutdown()
        server.server_close()


def test_pid_exists_detects_current_process():
    assert _pid_exists(os.getpid())


def test_cpu_pressure_parser(tmp_path: Path):
    pressure = tmp_path / "cpu"
    pressure.write_text("some avg10=12.34 avg60=1.00 total=3\n", encoding="utf-8")
    assert _cpu_pressure_avg10(pressure) == 12.34


def test_runtime_share_and_closed_loop_adjustment():
    policies = {1: {"class_id": 1}, 2: {"class_id": 3}}
    before = {1: {"runtime_ns": 100}, 2: {"runtime_ns": 100}}
    after = {1: {"runtime_ns": 1000}, 2: {"runtime_ns": 200}}
    shares = _runtime_shares(policies, before, after)
    assert round(shares["background_share"], 2) == 0.1
    assert choose_background_interval(2048, shares["background_share"], 1000, 0, True) == (
        "runtime_share_low",
        1024,
    )
    assert choose_background_interval(1024, 0.3, 1000, 0, True) == (
        "runtime_share_high",
        2048,
    )
    assert choose_background_interval(4096, 0, 0, 0, True) == (
        "awaiting_runtime_sample",
        4096,
    )
    assert choose_background_interval(4096, 0.2, 1000, 0, True, 0.3, 0.4) == (
        "runtime_share_low",
        2048,
    )
    assert choose_background_interval(64, 0.05, 1000, 0, True) == (
        "runtime_share_low",
        32,
    )
    assert choose_background_interval(64, 0.30, 1000, 0, True) == (
        "runtime_share_high",
        128,
    )
    assert choose_background_interval(512, 0, 0, 0, False) == (
        "uncontended",
        64,
    )


def test_daemon_cleanup_orphan_zero_metrics(tmp_path: Path):
    socket_path = tmp_path / "scx.sock"
    controller = FakeController()
    controller.cgroup_metrics = {
        11: {"enqueues": 0, "runs": 0, "runtime_ns": 0, "wait_ns": 0},
        12: {"enqueues": 1, "runs": 1, "runtime_ns": 2, "wait_ns": 3},
    }
    server = _ScxDaemonServer(socket_path, controller)
    try:
        assert server._cleanup_orphan_metrics({}, controller.cgroup_metrics) == 1
        assert 11 not in controller.cgroup_metrics
        assert 12 in controller.cgroup_metrics
        assert controller.removed_metric_ids == [11]
    finally:
        server.server_close()


def test_daemon_set_target_refreshes_telemetry(tmp_path: Path):
    socket_path = tmp_path / "scx.sock"
    controller = FakeController()
    server = _ScxDaemonServer(socket_path, controller)
    server.control_telemetry = {"target_low": 0.3, "target_high": 0.4, "reason": "test"}
    try:
        result = server.dispatch({"action": "set_target", "low": 0.12, "high": 0.25})
        assert result["target_background_share"] == {"low": 0.12, "high": 0.25}
        assert server.control_telemetry["target_low"] == 0.12
        assert server.control_telemetry["target_high"] == 0.25
    finally:
        server.server_close()
