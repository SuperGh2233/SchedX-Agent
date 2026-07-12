from schedx.agent.actions import Action
from schedx.agent.executor import SafeActionExecutor


class FakeCgroup:
    def __init__(self, dry_run=True):
        self.dry_run = dry_run
        self.calls = []

    def create_group(self, group):
        self.calls.append(("create_group", group))

    def add_pid(self, group, pid):
        self.calls.append(("add_pid", group, pid))

    def set_cpu_weight(self, group, weight):
        self.calls.append(("set_cpu_weight", group, weight))

    def set_cpu_max(self, group, quota):
        self.calls.append(("set_cpu_max", group, quota))

    def set_cpuset_mems(self, group, mems):
        self.calls.append(("set_cpuset_mems", group, mems))

    def set_cpuset_cpus(self, group, cpus):
        self.calls.append(("set_cpuset_cpus", group, cpus))

    def rollback(self):
        self.calls.append(("rollback",))
        return []


class FakeProcesses:
    def find_by_name(self, name):
        return [1234]


def test_dry_run_does_not_call_cgroup_writes():
    cgroup = FakeCgroup()
    executor = SafeActionExecutor(cgroup, FakeProcesses())
    results = executor.execute(
        [Action("set_cgroup_cpu_weight", "nginx", "process_name", 10000)]
    )
    assert results[0]["status"] == "dry_run"
    assert cgroup.calls == []


def test_dry_run_cpu_max_does_not_call_cgroup_writes():
    cgroup = FakeCgroup()
    executor = SafeActionExecutor(cgroup, FakeProcesses())
    results = executor.execute(
        [Action("set_cgroup_cpu_max", "nginx", "process_name", "50000 100000")]
    )
    assert results[0]["status"] == "dry_run"
    assert cgroup.calls == []


def test_non_dry_run_calls_cgroup_writes():
    cgroup = FakeCgroup(dry_run=False)
    executor = SafeActionExecutor(cgroup, FakeProcesses())
    results = executor.execute(
        [Action("set_cgroup_cpu_weight", "nginx", "process_name", 10000)]
    )
    assert results[0]["status"] == "ok"
    assert ("create_group", "pid-1234") in cgroup.calls
    assert ("add_pid", "pid-1234", 1234) in cgroup.calls
    assert ("set_cpu_weight", "pid-1234", 10000) in cgroup.calls


def test_cpuset_is_configured_before_pid_is_moved():
    cgroup = FakeCgroup(dry_run=False)
    executor = SafeActionExecutor(cgroup, FakeProcesses())

    results = executor.execute([Action("set_cpuset_cpus", "nginx", "process_name", "0,1")])

    assert results[0]["status"] == "ok"
    assert cgroup.calls.index(("set_cpuset_mems", "pid-1234", "0")) < cgroup.calls.index(("add_pid", "pid-1234", 1234))
    assert cgroup.calls.index(("set_cpuset_cpus", "pid-1234", "0,1")) < cgroup.calls.index(("add_pid", "pid-1234", 1234))
