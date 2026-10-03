"""Load, attach, update and inspect SchedX eBPF programs with bpftool."""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class EbpfProgType(Enum):
    """eBPF program families shipped by SchedX."""

    SCHED_TRACE = "sched_trace"
    NET_POLICY = "net_policy"
    RESOURCE_CTRL = "resource_ctrl"
    SECURITY_POLICY = "security_policy"


@dataclass
class EbpfStats:
    """Aggregated per-CPU statistics from pinned BPF maps."""

    sched_trace: dict[str, int] | None = None
    net_policy: dict[str, int] | None = None
    resource_ctrl: dict[str, int] | None = None
    security_policy: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sched_trace": self.sched_trace,
            "net_policy": self.net_policy,
            "resource_ctrl": self.resource_ctrl,
            "security_policy": self.security_policy,
        }


@dataclass
class EbpfProgramState:
    """Pinned kernel state for one eBPF object."""

    prog_type: EbpfProgType
    loaded: bool = False
    attached: bool = False
    pin_dir: Path | None = None
    attach_target: str | None = None
    attach_kind: str | None = None
    program_pin: Path | None = None
    map_paths: dict[str, Path] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.prog_type.value,
            "loaded": self.loaded,
            "attached": self.attached,
            "attach_target": self.attach_target,
            "maps": sorted(self.map_paths),
        }


class EbpfController:
    """Manage real BPF programs while keeping dangerous policies explicit.

    Tracepoint and LSM programs are attached with ``bpftool ... autoattach``;
    the resulting BPF links are pinned so they outlive the bpftool process.
    Network policy uses a cgroup v2 egress hook and is keyed by cgroup ID.
    """

    CLASS_UNKNOWN = 0
    CLASS_LATENCY = 1
    CLASS_BATCH = 2
    CLASS_BACKGROUND = 3

    RATE_LIMITS = {
        CLASS_LATENCY: 1_000_000_000,
        CLASS_BATCH: 100_000_000,
        CLASS_BACKGROUND: 10_000_000,
        CLASS_UNKNOWN: 500_000_000,
    }
    MEMORY_LIMITS = {
        CLASS_LATENCY: 4 * 1024 * 1024 * 1024,
        CLASS_BATCH: 2 * 1024 * 1024 * 1024,
        CLASS_BACKGROUND: 512 * 1024 * 1024,
        CLASS_UNKNOWN: 2 * 1024 * 1024 * 1024,
    }
    CPU_WEIGHTS = {
        CLASS_LATENCY: 8000,
        CLASS_BATCH: 1000,
        CLASS_BACKGROUND: 50,
        CLASS_UNKNOWN: 100,
    }
    IO_WEIGHTS = {
        CLASS_LATENCY: 900,
        CLASS_BATCH: 500,
        CLASS_BACKGROUND: 50,
        CLASS_UNKNOWN: 100,
    }

    _OBJECTS = {
        EbpfProgType.SCHED_TRACE: "sched_trace.bpf.o",
        EbpfProgType.NET_POLICY: "net_policy.bpf.o",
        EbpfProgType.RESOURCE_CTRL: "resource_ctrl.bpf.o",
        EbpfProgType.SECURITY_POLICY: "security_policy.bpf.o",
    }

    def __init__(
        self,
        ebpf_dir: Path | None = None,
        dry_run: bool = True,
        *,
        pin_root: Path = Path("/sys/fs/bpf/schedx"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        proc_root: Path = Path("/proc"),
    ) -> None:
        self.ebpf_dir = ebpf_dir or Path(__file__).parent.parent.parent / "ebpf"
        self.pin_root = pin_root
        self.cgroup_root = cgroup_root
        self.proc_root = proc_root
        self.dry_run = dry_run
        self.last_error = ""
        self._programs: dict[EbpfProgType, EbpfProgramState] = {}

    def is_available(self) -> bool:
        """Return whether bpffs and bpftool are usable."""
        if not self.pin_root.parent.exists():
            return False
        try:
            return self._run(["bpftool", "version"], timeout=5).returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def check_sched_ext(self) -> bool:
        return Path("/sys/kernel/sched_ext").exists()

    def security_lsm_available(self) -> bool:
        """BPF LSM must be enabled in the active kernel LSM list."""
        path = Path("/sys/kernel/security/lsm")
        if not path.exists():
            return False
        try:
            return "bpf" in {item.strip() for item in path.read_text().split(",")}
        except OSError:
            return False

    def get_status(self) -> dict[str, Any]:
        return {
            "available": self.is_available(),
            "sched_ext": self.check_sched_ext(),
            "bpf_lsm": self.security_lsm_available(),
            "programs": {
                prog_type.value: state.to_dict()
                for prog_type, state in self._programs.items()
            },
            "dry_run": self.dry_run,
            "last_error": self.last_error,
        }

    def has_pinned_programs(self) -> bool:
        return self.pin_root.exists() and any(self.pin_root.iterdir())

    def is_program_loaded(self, prog_type: EbpfProgType) -> bool:
        state = self._programs.get(prog_type)
        return bool(state and state.loaded)

    def cleanup_pinned(self) -> dict[str, bool]:
        """Adopt and unload programs left active by an earlier Agent process."""
        results: dict[str, bool] = {}
        for prog_type in reversed(tuple(EbpfProgType)):
            pin_dir = self.pin_root / prog_type.value
            if not pin_dir.exists():
                continue
            state = EbpfProgramState(
                prog_type=prog_type,
                loaded=True,
                attached=True,
                pin_dir=pin_dir,
                map_paths=self._discover_maps(pin_dir / "maps"),
            )
            if prog_type == EbpfProgType.NET_POLICY:
                state.program_pin = self._find_program_pin(state, "net_policy_egress")
            self._programs[prog_type] = state
            results[prog_type.value] = self.unload_program(prog_type)
        return results

    def load_program(self, prog_type: EbpfProgType) -> bool:
        """Load and pin all programs and maps from one BPF object."""
        state = EbpfProgramState(prog_type=prog_type, pin_dir=self.pin_root / prog_type.value)
        if self.dry_run:
            state.loaded = True
            self._programs[prog_type] = state
            return True

        if not self.is_available():
            return self._fail("bpftool or bpffs is unavailable")

        obj_file = self._get_object_path(prog_type)
        if not obj_file.exists():
            raise FileNotFoundError(f"BPF object not found: {obj_file}")

        try:
            self._remove_pin_tree(state.pin_dir)
            prog_dir = state.pin_dir / "progs"
            map_dir = state.pin_dir / "maps"
            prog_dir.mkdir(parents=True)
            map_dir.mkdir()
            result = self._run([
                "bpftool", "prog", "loadall", str(obj_file), str(prog_dir),
                "pinmaps", str(map_dir),
            ])
            if result.returncode:
                self._remove_pin_tree(state.pin_dir)
                return self._fail(result.stderr.strip() or "bpftool failed to load BPF object")

            state.loaded = True
            state.map_paths = self._discover_maps(map_dir)
            self._programs[prog_type] = state
            return True
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._remove_pin_tree(state.pin_dir)
            return self._fail(f"failed to load {prog_type.value}: {exc}")

    def attach_program(self, prog_type: EbpfProgType) -> bool:
        state = self._programs.get(prog_type)
        if state is None or not state.loaded:
            return False
        if self.dry_run:
            state.attached = True
            return True

        try:
            if prog_type == EbpfProgType.SCHED_TRACE:
                return self._attach_sched_trace(state)
            if prog_type == EbpfProgType.NET_POLICY:
                return self._attach_net_policy(state)
            if prog_type == EbpfProgType.RESOURCE_CTRL:
                return self._attach_resource_ctrl(state)
            if prog_type == EbpfProgType.SECURITY_POLICY:
                return self._attach_security_policy(state)
            return False
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._fail(f"failed to attach {prog_type.value}: {exc}")

    def unload_program(self, prog_type: EbpfProgType) -> bool:
        state = self._programs.get(prog_type)
        if state is None:
            return True
        if self.dry_run:
            del self._programs[prog_type]
            return True

        try:
            if state.attached and not self._detach_program(prog_type):
                return False
            if state.pin_dir:
                self._remove_pin_tree(state.pin_dir)
            if self.pin_root.exists() and not any(self.pin_root.iterdir()):
                self.pin_root.rmdir()
            del self._programs[prog_type]
            return True
        except OSError as exc:
            return self._fail(f"failed to unload {prog_type.value}: {exc}")

    def update_sched_policy(self, pid: int, class_id: int, weight: int | None = None) -> bool:
        """Scheduling policy belongs to sched_ext/cgroup, not the trace program."""
        if self.dry_run:
            return True
        return self._fail("sched_trace is observational; use ScxController or CgroupController")

    def update_net_policy(
        self,
        pid: int,
        class_id: int,
        rate_limit: int | None = None,
    ) -> bool:
        """Apply a network policy to the process's cgroup v2 identity."""
        if self.dry_run:
            return True
        cgroup_id = self._cgroup_id_for_pid(pid)
        if cgroup_id is None:
            return False
        return self.update_net_cgroup_policy(cgroup_id, class_id, rate_limit)

    def cgroup_id_for_pid(self, pid: int) -> int | None:
        """Resolve a PID to a policy-safe cgroup v2 identity."""
        if self.dry_run:
            return pid
        return self._cgroup_id_for_pid(pid)

    def update_net_cgroup_policy(
        self,
        cgroup_id: int,
        class_id: int,
        rate_limit: int | None = None,
        burst_size: int | None = None,
    ) -> bool:
        """Update network limits in bytes per second (legacy ``rate_limit`` name)."""
        if self.dry_run:
            return True
        rate = rate_limit if rate_limit is not None else self.RATE_LIMITS.get(class_id, 500_000_000)
        burst = burst_size if burst_size is not None else max(64 * 1024, rate // 10)
        if not 1 <= rate <= 0xffffffff or not 1 <= burst <= 0xffffffff:
            raise ValueError("network rate and burst must be positive uint32 values")
        priority = {self.CLASS_LATENCY: 7, self.CLASS_BATCH: 4}.get(class_id, 1)
        key = struct.pack("<Q", cgroup_id)
        value = struct.pack("<IIII", class_id, rate, burst, priority)
        return self._update_map(EbpfProgType.NET_POLICY, "net_policy_map", key, value)

    def update_resource_policy(
        self,
        cgroup_id: int,
        class_id: int,
        memory_limit: int | None = None,
        cpu_weight: int | None = None,
        io_weight: int | None = None,
    ) -> bool:
        """Update resource-policy metadata observed by the resource hook.

        Hard limits are still written by ``CgroupController``. This map records
        the policy selected by the Agent and lets the BPF hook count policy hits.
        """
        if self.dry_run:
            return True
        memory = memory_limit if memory_limit is not None else self.MEMORY_LIMITS.get(class_id, self.MEMORY_LIMITS[self.CLASS_UNKNOWN])
        cpu = cpu_weight if cpu_weight is not None else self.CPU_WEIGHTS.get(class_id, 100)
        io = io_weight if io_weight is not None else self.IO_WEIGHTS.get(class_id, 100)
        key = struct.pack("<Q", cgroup_id)
        value = struct.pack("<IIQII", class_id, 0, memory, cpu, io)
        return self._update_map(EbpfProgType.RESOURCE_CTRL, "res_policy_map", key, value)

    def remove_cgroup_policies(self, cgroup_id: int) -> bool:
        if self.dry_run:
            return True
        success = True
        for prog_type, names in ((EbpfProgType.NET_POLICY, ("net_policy_map", "net_bucket_map", "net_stats_map")),
                                 (EbpfProgType.RESOURCE_CTRL, ("res_policy_map",)),
                                 (EbpfProgType.SECURITY_POLICY, ("sec_exec_map",))):
            if not self.is_program_loaded(prog_type):
                continue
            for name in names:
                path = self._map_path(prog_type, name)
                if path is None:
                    continue
                result = self._run(["bpftool", "map", "delete", "pinned", str(path), "key", "hex", *self._hex(struct.pack("<Q", cgroup_id))])
                if result.returncode and "No such file" not in result.stderr:
                    success = False
        if self.is_program_loaded(EbpfProgType.SECURITY_POLICY):
            path = self._map_path(EbpfProgType.SECURITY_POLICY, "sec_policy_map")
            if path is not None:
                result = self._run(["bpftool", "-j", "map", "dump", "pinned", str(path)])
                if result.returncode:
                    return False
                for item in json.loads(result.stdout):
                    key = item.get("key")
                    if isinstance(key, list):
                        raw = bytes(int(value, 16) if isinstance(value, str) else value for value in key)
                        identity = struct.unpack("<QQQ", raw)
                    else:
                        key = key if isinstance(key, dict) else item.get("formatted", {}).get("key", {})
                        identity = tuple(int(key[name]) for name in ("cgroup_id", "device", "inode"))
                        raw = struct.pack("<QQQ", *identity)
                    if identity[0] == cgroup_id:
                        deleted = self._run(["bpftool", "map", "delete", "pinned", str(path), "key", "hex", *self._hex(raw)])
                        if deleted.returncode and "No such file" not in deleted.stderr:
                            success = False
        return success

    def update_security_policy(
        self,
        cgroup_id: int,
        executable: str | Path,
        *,
        deny_exec: bool = False,
        audit_only: bool = True,
    ) -> bool:
        """Set a policy for one executable inode inside one cgroup."""
        if self.dry_run:
            return True
        try:
            file_stat = Path(executable).stat()
        except OSError as exc:
            return self._fail(f"cannot stat security policy executable: {exc}")
        # ``inode->i_sb->s_dev`` uses the kernel's 20-bit-minor dev_t layout,
        # while stat(2) exposes the libc encoding. Convert before keying the
        # BPF map so the userspace and LSM identities are byte-for-byte equal.
        kernel_device = self._kernel_device_id(file_stat.st_dev)
        key = struct.pack("<QQQ", cgroup_id, kernel_device, file_stat.st_ino)
        value = struct.pack("<II", int(deny_exec), int(audit_only))
        return self._update_map(EbpfProgType.SECURITY_POLICY, "sec_policy_map", key, value)

    def get_stats(self) -> EbpfStats:
        if self.dry_run:
            return EbpfStats()
        stats = EbpfStats()
        for prog_type, state in self._programs.items():
            if not state.loaded:
                continue
            try:
                if prog_type == EbpfProgType.SCHED_TRACE:
                    stats.sched_trace = self._get_sched_trace_stats()
                elif prog_type == EbpfProgType.NET_POLICY:
                    stats.net_policy = self._get_net_policy_stats()
                elif prog_type == EbpfProgType.RESOURCE_CTRL:
                    stats.resource_ctrl = self._get_resource_ctrl_stats()
                elif prog_type == EbpfProgType.SECURITY_POLICY:
                    stats.security_policy = self._get_security_policy_stats()
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return stats

    def _get_object_path(self, prog_type: EbpfProgType) -> Path:
        return self.ebpf_dir / "build" / self._OBJECTS[prog_type]

    def _attach_sched_trace(self, state: EbpfProgramState) -> bool:
        """Attach scheduler tracepoints and pin their BPF links."""
        return self._reload_autoattached(state)

    def _attach_net_policy(self, state: EbpfProgramState) -> bool:
        """Attach the cgroup_skb egress program to the cgroup v2 root."""
        if not (self.cgroup_root / "cgroup.controllers").exists():
            return self._fail(f"cgroup v2 is unavailable at {self.cgroup_root}")
        prog_pin = self._find_program_pin(state, "net_policy_egress")
        if prog_pin is None:
            return self._fail("net_policy_egress pin was not created")

        for attach_kind in ("cgroup_inet_egress", "egress"):
            result = self._run([
                "bpftool", "cgroup", "attach", str(self.cgroup_root), attach_kind,
                "pinned", str(prog_pin), "multi",
            ])
            if result.returncode == 0:
                state.attached = True
                state.attach_target = str(self.cgroup_root)
                state.attach_kind = attach_kind
                state.program_pin = prog_pin
                return True
        return self._fail(result.stderr.strip() or "failed to attach cgroup egress program")

    def _attach_resource_ctrl(self, state: EbpfProgramState) -> bool:
        """Attach stable scheduler tracepoints used for resource telemetry."""
        return self._reload_autoattached(state)

    def _attach_security_policy(self, state: EbpfProgramState) -> bool:
        """Attach the independent BPF LSM execution policy."""
        if not self.security_lsm_available():
            return self._fail("BPF LSM is not enabled in /sys/kernel/security/lsm")
        return self._reload_autoattached(state)

    def _reload_autoattached(self, state: EbpfProgramState) -> bool:
        if state.pin_dir is None:
            return False
        obj_file = self._get_object_path(state.prog_type)
        self._remove_pin_tree(state.pin_dir)
        link_dir = state.pin_dir / "links"
        map_dir = state.pin_dir / "maps"
        link_dir.mkdir(parents=True)
        map_dir.mkdir()
        result = self._run([
            "bpftool", "prog", "loadall", str(obj_file), str(link_dir),
            "pinmaps", str(map_dir), "autoattach",
        ])
        if result.returncode:
            self._remove_pin_tree(state.pin_dir)
            return self._fail(result.stderr.strip() or "bpftool autoattach failed")
        state.loaded = True
        state.attached = True
        state.attach_target = "kernel-declared hooks"
        state.map_paths = self._discover_maps(map_dir)
        return True

    def _detach_program(self, prog_type: EbpfProgType) -> bool:
        state = self._programs.get(prog_type)
        if state is None or not state.attached:
            return True
        if prog_type == EbpfProgType.NET_POLICY and state.program_pin:
            attach_kinds = (
                (state.attach_kind,)
                if state.attach_kind
                else ("cgroup_inet_egress", "egress")
            )
            errors = []
            for attach_kind in attach_kinds:
                result = self._run([
                    "bpftool", "cgroup", "detach", str(self.cgroup_root), attach_kind,
                    "pinned", str(state.program_pin),
                ])
                if result.returncode == 0:
                    break
                errors.append(result.stderr.strip())
            else:
                message = "; ".join(error for error in errors if error)
                if "No such file" not in message and "No such process" not in message:
                    return self._fail(message or "failed to detach network policy")
        state.attached = False
        return True

    def _get_sched_trace_stats(self) -> dict[str, int]:
        values = self._lookup_percpu(EbpfProgType.SCHED_TRACE, "global_stats", "<QQQI4x")
        return dict(zip(("total_wakeups", "total_switches", "total_latency_ns", "active_tasks"), values))

    def _get_net_policy_stats(self) -> dict[str, int]:
        values = self._lookup_percpu(EbpfProgType.NET_POLICY, "net_global_map", "<QQQQQ")
        return dict(zip(("total_packets", "total_bytes", "dropped_packets", "dropped_bytes", "overlimit_allowed_packets"), values))

    def _get_resource_ctrl_stats(self) -> dict[str, int]:
        values = self._lookup_percpu(EbpfProgType.RESOURCE_CTRL, "res_global_map", "<QQQ")
        return dict(zip(("sched_switches", "policy_hits", "process_exits"), values))

    def _get_security_policy_stats(self) -> dict[str, int]:
        values = self._lookup_percpu(EbpfProgType.SECURITY_POLICY, "sec_stats_map", "<QQ")
        return dict(zip(("exec_checks", "exec_denied"), values))

    def _lookup_percpu(self, prog_type: EbpfProgType, map_name: str, fmt: str) -> tuple[int, ...]:
        map_path = self._map_path(prog_type, map_name)
        if map_path is None:
            raise OSError(f"map is not pinned: {map_name}")
        result = self._run([
            "bpftool", "-j", "map", "lookup", "pinned", str(map_path),
            "key", "hex", "00", "00", "00", "00",
        ])
        if result.returncode:
            raise OSError(result.stderr.strip())
        payload = json.loads(result.stdout)
        entries = payload.get("values") or [payload]
        totals = [0] * len(struct.unpack(fmt, bytes(struct.calcsize(fmt))))
        for entry in entries:
            raw = entry.get("value", []) if isinstance(entry, dict) else []
            data = bytes(self._json_byte(item) for item in raw)
            if len(data) < struct.calcsize(fmt):
                continue
            for index, value in enumerate(struct.unpack(fmt, data[: struct.calcsize(fmt)])):
                totals[index] += value
        return tuple(totals)

    def _update_map(self, prog_type: EbpfProgType, name: str, key: bytes, value: bytes) -> bool:
        map_path = self._map_path(prog_type, name)
        if map_path is None:
            return self._fail(f"map is not pinned: {name}")
        result = self._run([
            "bpftool", "map", "update", "pinned", str(map_path),
            "key", "hex", *self._hex(key), "value", "hex", *self._hex(value),
        ])
        if result.returncode:
            return self._fail(result.stderr.strip() or f"failed to update {name}")
        return True

    def _map_path(self, prog_type: EbpfProgType, name: str) -> Path | None:
        state = self._programs.get(prog_type)
        if state is None or not state.loaded:
            self.last_error = f"{prog_type.value} is not loaded"
            return None
        path = state.map_paths.get(name)
        if path is None and state.pin_dir:
            candidate = state.pin_dir / "maps" / name
            if candidate.exists():
                state.map_paths[name] = candidate
                path = candidate
        return path

    def _cgroup_id_for_pid(self, pid: int) -> int | None:
        path = self.proc_root / str(pid) / "cgroup"
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                hierarchy, controllers, relative = line.split(":", 2)
                if hierarchy == "0" and not controllers:
                    if relative in ("", "/"):
                        return self._fail_value("refusing a network policy on the cgroup root")
                    if not (relative.startswith("/schedx/") or relative.endswith(".service")):
                        return self._fail_value(
                            "refusing a PID policy on a shared cgroup; move the workload "
                            "to /sys/fs/cgroup/schedx first"
                        )
                    return (self.cgroup_root / relative.lstrip("/")).stat().st_ino
        except (OSError, ValueError) as exc:
            return self._fail_value(f"cannot resolve cgroup for pid {pid}: {exc}")
        return self._fail_value(f"pid {pid} is not in a cgroup v2 hierarchy")

    def _find_program_pin(self, state: EbpfProgramState, expected: str) -> Path | None:
        if state.pin_dir is None:
            return None
        prog_dir = state.pin_dir / "progs"
        if not prog_dir.exists():
            return None
        pins = [path for path in prog_dir.iterdir() if path.is_file()]
        for path in pins:
            if expected.startswith(path.name) or path.name.startswith(expected):
                return path
        return pins[0] if len(pins) == 1 else None

    @staticmethod
    def _discover_maps(map_dir: Path) -> dict[str, Path]:
        if not map_dir.exists():
            return {}
        return {path.name: path for path in map_dir.iterdir() if path.is_file()}

    def _remove_pin_tree(self, path: Path | None) -> None:
        if path is None or not path.exists():
            return
        root = self.pin_root.absolute()
        target = path.absolute()
        if target != root and root not in target.parents:
            raise ValueError(f"refusing to remove path outside pin root: {path}")
        for child in list(path.iterdir()):
            if child.is_dir():
                self._remove_pin_tree(child)
            else:
                child.unlink()
        path.rmdir()

    @staticmethod
    def _hex(data: bytes) -> list[str]:
        return [f"{byte:02x}" for byte in data]

    @staticmethod
    def _kernel_device_id(device: int) -> int:
        """Convert Linux userspace dev_t encoding to the kernel layout."""
        major = ((device >> 8) & 0xFFF) | ((device >> 32) & ~0xFFF)
        minor = (device & 0xFF) | ((device >> 12) & ~0xFF)
        return (major << 20) | minor

    @staticmethod
    def _json_byte(value: int | str) -> int:
        if isinstance(value, int):
            return value
        return int(value.removeprefix("0x"), 16)

    @staticmethod
    def _run(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    def _fail(self, message: str) -> bool:
        self.last_error = message
        print(f"eBPF: {message}", file=sys.stderr)
        return False

    def _fail_value(self, message: str) -> None:
        self._fail(message)
        return None

    @staticmethod
    def classify_to_ebpf_class(workload_type: str) -> int:
        return {
            "latency_sensitive": EbpfController.CLASS_LATENCY,
            "batch_compute": EbpfController.CLASS_BATCH,
            "background_noise": EbpfController.CLASS_BACKGROUND,
            "unknown": EbpfController.CLASS_UNKNOWN,
        }.get(workload_type, EbpfController.CLASS_UNKNOWN)
