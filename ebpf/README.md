# SchedX-Agent eBPF Hooks

These programs extend the Agent with kernel observation and narrowly scoped
policy hooks. They do not replace Linux subsystems that already enforce the
same resource correctly.

| Object | Hook | Responsibility |
|---|---|---|
| `sched_trace.bpf.o` | scheduler tracepoints | Measure wakeup and scheduling latency |
| `net_policy.bpf.o` | `cgroup_skb/egress` | Per-cgroup token-bucket network policy |
| `resource_ctrl.bpf.o` | scheduler tracepoints | Count cgroup policy activity |
| `security_policy.bpf.o` | `lsm/bprm_check_security` | Audit or deny one executable in one cgroup |

CPU scheduling is performed by `sched_ext/scx` and hard CPU, memory and I/O
limits are performed by cgroup v2. The resource eBPF program is telemetry, not
a second and less reliable resource controller.

## Build on openEuler 24.03 LTS SP4

```bash
dnf install -y clang llvm make bpftool libbpf-devel elfutils-libelf-devel
make -C ebpf check
make -C ebpf all
```

The Makefile generates `build/vmlinux.h` from the running kernel's BTF at
`/sys/kernel/btf/vmlinux`, then builds all four CO-RE objects. x86_64 and
aarch64 target macros are selected automatically.

## Lifecycle

`schedx.controllers.ebpf_controller.EbpfController` uses only `bpftool`:

1. load programs and pin maps under `/sys/fs/bpf/schedx/<type>/`;
2. attach tracepoint/LSM programs with `autoattach` and pin their BPF links;
3. attach network policy to the cgroup v2 root with `bpftool cgroup attach`;
4. update pinned maps with exact binary key/value layouts;
5. detach and remove pins during unload.

Pinned links are important: without them, tracepoint and LSM attachments would
disappear as soon as the short-lived `bpftool` process exits.

## Policy Safety

- Network policies are keyed by cgroup ID, not PID. A packet may be processed
  outside the originating process context, so PID attribution at TC is unsafe.
- The cgroup v2 root is never accepted as a per-process network-policy target.
- Security is default-allow. A key contains cgroup ID, device ID and inode, so
  one policy cannot accidentally block every executable in a workload.
- `audit_only=true` records matching executions without denying them.
- BPF LSM is optional. Check `cat /sys/kernel/security/lsm`; it must contain
  `bpf`. When it is absent, the controller reports unsupported rather than
  returning a false success.

## Python Example

```python
from pathlib import Path
from schedx.controllers.ebpf_controller import EbpfController, EbpfProgType

controller = EbpfController(dry_run=False)
for kind in EbpfProgType:
    if controller.load_program(kind):
        controller.attach_program(kind)

# A process API resolves PID -> cgroup v2 ID before updating the map.
controller.update_net_policy(pid=1234, class_id=controller.CLASS_BACKGROUND)

# Resource values are policy telemetry; CgroupController performs hard limits.
controller.update_resource_policy(cgroup_id=42, class_id=controller.CLASS_BATCH)

# Audit one executable in one cgroup. Set deny_exec=True and audit_only=False
# only in an isolated test cgroup.
controller.update_security_policy(42, Path("/usr/bin/true"))
```

## Verification

```bash
bpftool feature probe kernel
bpftool link show
bpftool cgroup show /sys/fs/cgroup effective
bpftool map show
```

Run security enforcement tests only inside a disposable cgroup. A production
workload should start in audit-only mode and graduate to enforcement after the
Agent's verification step succeeds.
