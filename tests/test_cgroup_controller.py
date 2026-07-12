import json
from pathlib import Path

from schedx.controllers.cgroup_controller import CgroupController, RollbackEntry


def test_cgroup_dry_run_does_not_create_group(tmp_path: Path):
    root = tmp_path / "cgroup"
    controller = CgroupController(root=root, dry_run=True)
    path = controller.create_group("demo")
    assert path == root / "schedx" / "demo"
    assert not path.exists()


def test_cgroup_pid_path_uses_base_group(tmp_path: Path):
    root = tmp_path / "cgroup"
    controller = CgroupController(root=root, dry_run=True)
    assert controller.group_path("pid-123") == root / "schedx" / "pid-123"


def test_cleanup_removes_empty_pid_and_base_groups(tmp_path: Path):
    root = tmp_path / "cgroup"
    pid_group = root / "schedx" / "pid-123"
    pid_group.mkdir(parents=True)
    controller = CgroupController(root=root, dry_run=False)
    results = controller.cleanup_empty_groups()
    assert not pid_group.exists()
    assert not (root / "schedx").exists()
    assert {"path": str(pid_group), "status": "removed"} in results
    assert {"path": str(root / "schedx"), "status": "removed"} in results


def test_cleanup_does_not_force_remove_non_empty_group(tmp_path: Path):
    root = tmp_path / "cgroup"
    pid_group = root / "schedx" / "pid-123"
    pid_group.mkdir(parents=True)
    (pid_group / "cgroup.procs").write_text("123\n", encoding="utf-8")
    controller = CgroupController(root=root, dry_run=False)
    results = controller.cleanup_empty_groups()
    assert pid_group.exists()
    assert {"path": str(pid_group), "status": "skipped", "reason": "process_still_alive"} in results


def test_cleanup_treats_cgroup_control_files_as_removable_base(tmp_path: Path):
    root = tmp_path / "cgroup"
    base = root / "schedx"
    base.mkdir(parents=True)
    for name in (
        "cgroup.procs",
        "cpu.weight",
        "cpu.max",
        "memory.current",
        "cgroup.controllers",
        "cgroup.subtree_control",
    ):
        (base / name).write_text("", encoding="utf-8")
    controller = CgroupController(root=root, dry_run=False)
    removable, reason = controller._base_group_removable(base)
    assert removable
    assert reason == ""


def test_cleanup_keeps_base_when_pid_child_exists(tmp_path: Path):
    root = tmp_path / "cgroup"
    base = root / "schedx"
    (base / "pid-123").mkdir(parents=True)
    (base / "cgroup.procs").write_text("", encoding="utf-8")
    controller = CgroupController(root=root, dry_run=False)
    removable, reason = controller._base_group_removable(base)
    assert not removable
    assert reason == "not_empty"
    assert base.exists()


def test_cleanup_keeps_base_when_base_procs_non_empty(tmp_path: Path):
    root = tmp_path / "cgroup"
    base = root / "schedx"
    base.mkdir(parents=True)
    (base / "cgroup.procs").write_text("123\n", encoding="utf-8")
    controller = CgroupController(root=root, dry_run=False)
    removable, reason = controller._base_group_removable(base)
    assert not removable
    assert reason == "process_still_alive"
    assert base.exists()


def test_cpuset_dry_run_does_not_create_group(tmp_path: Path):
    root = tmp_path / "cgroup"
    controller = CgroupController(root=root, dry_run=True)

    controller.set_cpuset_mems("pid-123", "0")
    controller.set_cpuset_cpus("pid-123", "0,1")

    assert not (root / "schedx").exists()


def test_add_pid_records_original_cgroup_for_rollback(tmp_path: Path, monkeypatch):
    root = tmp_path / "cgroup"
    original = root / "system.slice" / "nginx.service"
    target = root / "schedx" / "pid-123"
    original.mkdir(parents=True)
    target.mkdir(parents=True)
    (target / "cgroup.procs").write_text("", encoding="utf-8")
    controller = CgroupController(root=root, rollback_file=tmp_path / "rollback.json", dry_run=False)
    monkeypatch.setattr(controller, "create_group", lambda group: target)
    monkeypatch.setattr(controller, "_current_cgroup_path", lambda pid: original)
    monkeypatch.setattr(controller, "validate_writable", lambda: None)

    controller.add_pid("pid-123", 123)

    entries = controller._load_rollback()
    assert entries == [RollbackEntry(str(original), "cgroup.procs", "123")]


def test_rollback_continues_when_target_process_exited(tmp_path: Path):
    rollback_file = tmp_path / "rollback.json"
    rollback_file.write_text(
        json.dumps([{"path": str(tmp_path / "missing"), "file": "cgroup.procs", "previous": "123"}]),
        encoding="utf-8",
    )
    controller = CgroupController(root=tmp_path / "cgroup", rollback_file=rollback_file, dry_run=False)

    results = controller.rollback()

    assert any(isinstance(item, dict) and item.get("reason") == "target_process_exited" for item in results)
    assert not rollback_file.exists()
