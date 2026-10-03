import struct
from pathlib import Path
from types import SimpleNamespace

from schedx.controllers.ebpf_controller import (
    EbpfController,
    EbpfProgramState,
    EbpfProgType,
)
from schedx.probes.ebpf_probe import EbpfProbe, SecurityPolicyHook


def completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def make_controller(tmp_path: Path) -> EbpfController:
    ebpf_dir = tmp_path / "ebpf"
    build = ebpf_dir / "build"
    build.mkdir(parents=True)
    for name in (
        "sched_trace.bpf.o",
        "net_policy.bpf.o",
        "resource_ctrl.bpf.o",
        "security_policy.bpf.o",
    ):
        (build / name).write_bytes(b"object")
    pin_parent = tmp_path / "bpffs"
    pin_parent.mkdir()
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.controllers").write_text("cpu memory", encoding="utf-8")
    return EbpfController(
        ebpf_dir=ebpf_dir,
        dry_run=False,
        pin_root=pin_parent / "schedx",
        cgroup_root=cgroup,
        proc_root=tmp_path / "proc",
    )


def fake_bpftool(controller: EbpfController, calls: list[list[str]]):
    def run(args, timeout=30):
        calls.append(args)
        if args[:3] == ["bpftool", "prog", "loadall"]:
            destination = Path(args[4])
            map_dir = Path(args[args.index("pinmaps") + 1])
            destination.mkdir(parents=True, exist_ok=True)
            map_dir.mkdir(parents=True, exist_ok=True)
            object_name = Path(args[3]).name
            if object_name == "net_policy.bpf.o":
                (destination / "net_policy_egress").touch()
                (map_dir / "net_policy_map").touch()
                (map_dir / "net_global_map").touch()
            elif object_name == "resource_ctrl.bpf.o":
                (destination / "trace_resource").touch()
                (map_dir / "res_policy_map").touch()
                (map_dir / "res_global_map").touch()
            elif object_name == "security_policy.bpf.o":
                (destination / "check_exec").touch()
                (map_dir / "sec_policy_map").touch()
                (map_dir / "sec_stats_map").touch()
            else:
                (destination / "trace_sched").touch()
                (map_dir / "global_stats").touch()
        return completed(stdout="bpftool v7")

    return run


def test_trace_attach_reloads_with_autoattach_and_pins_links(tmp_path, monkeypatch):
    controller = make_controller(tmp_path)
    calls = []
    monkeypatch.setattr(controller, "_run", fake_bpftool(controller, calls))

    assert controller.load_program(EbpfProgType.SCHED_TRACE)
    assert controller.attach_program(EbpfProgType.SCHED_TRACE)

    autoattach = [call for call in calls if "autoattach" in call]
    assert len(autoattach) == 1
    state = controller._programs[EbpfProgType.SCHED_TRACE]
    assert state.attached
    assert state.attach_target == "kernel-declared hooks"
    assert "global_stats" in state.map_paths


def test_network_attach_targets_cgroup_v2_egress(tmp_path, monkeypatch):
    controller = make_controller(tmp_path)
    calls = []
    monkeypatch.setattr(controller, "_run", fake_bpftool(controller, calls))

    assert controller.load_program(EbpfProgType.NET_POLICY)
    assert controller.attach_program(EbpfProgType.NET_POLICY)

    attach = next(call for call in calls if call[:3] == ["bpftool", "cgroup", "attach"])
    assert attach[3] == str(controller.cgroup_root)
    assert attach[4] == "cgroup_inet_egress"
    assert attach[-1] == "multi"


def test_network_policy_updates_u64_cgroup_key_and_real_map(tmp_path, monkeypatch):
    controller = make_controller(tmp_path)
    map_path = tmp_path / "net_policy_map"
    map_path.touch()
    controller._programs[EbpfProgType.NET_POLICY] = EbpfProgramState(
        EbpfProgType.NET_POLICY,
        loaded=True,
        map_paths={"net_policy_map": map_path},
    )
    calls = []
    monkeypatch.setattr(controller, "_run", lambda args, timeout=30: calls.append(args) or completed())
    monkeypatch.setattr(controller, "_cgroup_id_for_pid", lambda pid: 0x1122334455667788)

    assert controller.update_net_policy(123, controller.CLASS_BACKGROUND, rate_limit=10_000)

    command = calls[-1]
    key_start = command.index("key") + 2
    value_start = command.index("value") + 2
    key = bytes.fromhex(" ".join(command[key_start : value_start - 2]))
    value = bytes.fromhex(" ".join(command[value_start:]))
    assert struct.unpack("<Q", key) == (0x1122334455667788,)
    assert struct.unpack("<IIII", value)[:2] == (controller.CLASS_BACKGROUND, 10_000)


def test_resource_policy_map_abi_matches_bpf_structure(tmp_path, monkeypatch):
    controller = make_controller(tmp_path)
    map_path = tmp_path / "res_policy_map"
    map_path.touch()
    controller._programs[EbpfProgType.RESOURCE_CTRL] = EbpfProgramState(
        EbpfProgType.RESOURCE_CTRL,
        loaded=True,
        map_paths={"res_policy_map": map_path},
    )
    calls = []
    monkeypatch.setattr(controller, "_run", lambda args, timeout=30: calls.append(args) or completed())

    assert controller.update_resource_policy(77, controller.CLASS_BATCH, 4096, 900, 300)

    command = calls[-1]
    value = bytes.fromhex(" ".join(command[command.index("value") + 2 :]))
    assert len(value) == 24
    assert struct.unpack("<IIQII", value) == (controller.CLASS_BATCH, 0, 4096, 900, 300)


def test_security_policy_is_inode_scoped_and_defaults_to_audit(tmp_path, monkeypatch):
    controller = make_controller(tmp_path)
    map_path = tmp_path / "sec_policy_map"
    map_path.touch()
    executable = tmp_path / "demo-command"
    executable.write_text("demo", encoding="utf-8")
    controller._programs[EbpfProgType.SECURITY_POLICY] = EbpfProgramState(
        EbpfProgType.SECURITY_POLICY,
        loaded=True,
        map_paths={"sec_policy_map": map_path},
    )
    calls = []
    monkeypatch.setattr(controller, "_run", lambda args, timeout=30: calls.append(args) or completed())

    assert controller.update_security_policy(99, executable)

    command = calls[-1]
    key_start = command.index("key") + 2
    value_start = command.index("value") + 2
    key = bytes.fromhex(" ".join(command[key_start : value_start - 2]))
    value = bytes.fromhex(" ".join(command[value_start:]))
    stat = executable.stat()
    kernel_device = controller._kernel_device_id(stat.st_dev)
    assert struct.unpack("<QQQ", key) == (99, kernel_device, stat.st_ino)
    assert struct.unpack("<II", value) == (0, 1)


def test_security_hook_is_part_of_dry_run_probe(monkeypatch):
    probe = EbpfProbe(dry_run=True)
    monkeypatch.setattr(probe.controller, "is_available", lambda: True)

    loaded = probe.load_all()
    attached = probe.attach_all()

    assert isinstance(probe.security_policy, SecurityPolicyHook)
    assert loaded["security-policy"]
    assert attached["security-policy"]
    assert probe.security_policy._get_prog_type() == EbpfProgType.SECURITY_POLICY
