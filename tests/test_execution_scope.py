import pytest

from schedx.agent.actions import Action
from schedx.agent.executor import SafeActionExecutor
from schedx.controllers.cgroup_controller import CgroupController


class Processes:
    def find_by_name(self, name):
        return [101, 202, 303]


def executor(tmp_path, scope):
    return SafeActionExecutor(CgroupController(root=tmp_path, dry_run=True), Processes(), scope_pids=scope)


def test_name_resolution_cannot_mutate_same_named_processes_outside_scope(tmp_path):
    action = Action("set_cgroup_cpu_weight", "nginx", "process_name", 8000)
    result = executor(tmp_path, [202]).execute([action], dry_run=True)
    assert [row["pid"] for row in result] == [202]


def test_empty_scope_does_not_fall_back_to_global_name_resolution(tmp_path):
    action = Action("set_cgroup_cpu_weight", "nginx", "process_name", 8000)
    result = executor(tmp_path, []).execute([action], dry_run=True)
    assert not any(row.get("pid") for row in result)


def test_explicit_pid_outside_scope_fails_before_a_later_action(tmp_path):
    result = executor(tmp_path, [202]).execute([
        Action("set_cgroup_cpu_weight", "101", "pid", 8000),
        Action("set_cgroup_cpu_weight", "202", "pid", 8000),
    ], dry_run=True)
    assert result[-1]["status"] == "failed"
    assert not any(row.get("pid") == 202 for row in result)


def test_direct_cgroup_move_also_checks_pid_scope(tmp_path):
    result = executor(tmp_path, [202]).execute([Action("move_pid_to_cgroup", "worker", "cgroup", 101)], dry_run=True)
    assert result[-1]["status"] == "failed"


def test_default_unscoped_operation_preserves_existing_name_behavior(tmp_path):
    result = executor(tmp_path, None).execute([Action("set_cgroup_cpu_weight", "nginx", "process_name", 8000)], dry_run=True)
    assert [row["pid"] for row in result] == [101, 202, 303]


@pytest.mark.parametrize("scope", [[True], ["202"], [-1], "202"])
def test_invalid_execution_scope_is_rejected(tmp_path, scope):
    with pytest.raises(ValueError):
        executor(tmp_path, scope)
