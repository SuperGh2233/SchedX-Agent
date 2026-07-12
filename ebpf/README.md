# SchedX-Agent eBPF Programs

This directory contains eBPF programs for enhanced resource control in SchedX-Agent.

## Overview

The eBPF programs provide:

1. **sched_trace** - Scheduling latency tracing
2. **net_policy** - Network policy enforcement via TC
3. **resource_ctrl** - Resource control monitoring

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    SchedX-Agent (Python)                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │  Probe   │→│ Classify │→│  Policy  │→│   Act    │    │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘    │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              EbpfController (Python)                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                  │
│  │  Load    │→│  Attach  │→│  Policy  │                  │
│  └──────────┘  └──────────┘  └──────────┘                  │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    eBPF Programs (Kernel)                     │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  sched_trace.bpf.o    │  net_policy.bpf.o            │  │
│  │  - sched_switch       │  - TC egress classifier      │  │
│  │  - sched_wakeup       │  - TC ingress classifier     │  │
│  │  - latency histograms │  - rate limiting             │  │
│  ├──────────────────────────────────────────────────────┤  │
│  │  resource_ctrl.bpf.o                                 │  │
│  │  - memory tracking    │  - cgroup monitoring         │  │
│  │  - I/O tracking       │  - OOM notification          │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## eBPF Programs

### 1. sched_trace.bpf.c

Traces scheduling events to measure latency per workload.

**Features:**
- Hooks into `sched_switch` and `sched_wakeup` tracepoints
- Tracks scheduling latency per task
- Generates latency histograms
- Provides per-task statistics

**BPF Maps:**
- `task_info_map` - Per-task tracking information
- `latency_histogram` - Latency distribution (per-CPU)
- `global_stats` - Global scheduling statistics
- `events` - Ring buffer for real-time events

### 2. net_policy.bpf.c

Enforces network policies using TC (Traffic Control).

**Features:**
- Per-task rate limiting
- Traffic classification by workload type
- Token bucket algorithm for smooth rate limiting
- Priority queuing for latency-sensitive tasks

**BPF Maps:**
- `net_policy_map` - Per-task network policy
- `token_bucket_map` - Per-task token bucket state
- `net_stats_map` - Per-task network statistics
- `global_net_stats_map` - Global network statistics

**Rate Limits:**
| Class | Rate Limit | Burst Size |
|-------|-----------|------------|
| Latency | 1 GB/s | 10 MB |
| Batch | 100 MB/s | 1 MB |
| Background | 10 MB/s | 100 KB |

### 3. resource_ctrl.bpf.c

Monitors and controls resource usage per cgroup.

**Features:**
- Memory allocation tracking
- I/O bandwidth monitoring
- CPU usage accounting
- OOM notification
- Resource limit enforcement

**BPF Maps:**
- `resource_policy_map` - Per-cgroup resource policy
- `resource_usage_map` - Per-cgroup resource usage
- `task_cgroup_map` - Task to cgroup mapping
- `resource_events` - Ring buffer for resource events

## Building

### Prerequisites

```bash
# openEuler 24.03-LTS-SP3
sudo dnf install -y gcc clang llvm llvm-tools make cmake git
sudo dnf install -y kernel-devel kernel-headers kernel-debuginfo
sudo dnf install -y libbpf-devel libbpf elfutils-libelf-devel zlib-devel
sudo dnf install -y kernel-tools  # for bpftool
```

### Build

```bash
cd ebpf
make              # Build all BPF programs
make sched_trace  # Build sched_trace only
make net_policy   # Build net_policy only
make resource_ctrl # Build resource_ctrl only
```

### Install

```bash
sudo make install  # Install to /usr/lib/schedx/ebpf
```

## Usage

### From Python (EbpfController)

```python
from schedx.controllers.ebpf_controller import EbpfController, EbpfProgType

# Initialize controller
controller = EbpfController(dry_run=False)

# Load programs
controller.load_program(EbpfProgType.SCHED_TRACE)
controller.load_program(EbpfProgType.NET_POLICY)
controller.load_program(EbpfProgType.RESOURCE_CTRL)

# Attach to hooks
controller.attach_program(EbpfProgType.SCHED_TRACE)
controller.attach_program(EbpfProgType.NET_POLICY)
controller.attach_program(EbpfProgType.RESOURCE_CTRL)

# Update policies
controller.update_sched_policy(pid=1234, class_id=1, weight=10000)
controller.update_net_policy(pid=1234, class_id=1, rate_limit=1000000000)
controller.update_resource_policy(cgroup_id=42, class_id=1, memory_limit=4*1024*1024*1024)

# Get statistics
stats = controller.get_stats()
```

### From SchedX-Agent CLI

```bash
# Load and attach eBPF programs (via agent pipeline)
schedx optimize --target stress-ng --mode isolate_background

# Check eBPF status
schedx status
```

### Manual Testing

```bash
# Load sched_trace manually
sudo bpftool prog load build/sched_trace.bpf.o /sys/fs/bpf/schedx/sched_trace

# List loaded programs
sudo bpftool prog list

# Dump program statistics
sudo bpftool map dump name global_stats
```

## Integration with Agent Pipeline

The eBPF programs are integrated into the SchedX-Agent pipeline:

1. **probe** - Collects process information
2. **analyze** - Classifies workloads
3. **policy** - Plans optimization actions
4. **ebpf_load** - Loads eBPF programs
5. **ebpf_attach** - Attaches to kernel hooks
6. **ebpf_policy** - Applies workload policies
7. **scx** - Applies scx scheduler policies (if available)
8. **act** - Applies cgroup controls
9. **verify** - Verifies optimization results
10. **report** - Generates optimization report

## Performance Considerations

- **Overhead**: eBPF programs run in kernel space with minimal overhead
- **Sampling**: sched_trace samples 1 in 100 events to reduce overhead
- **Per-CPU maps**: Statistics use per-CPU maps to avoid contention
- **Ring buffers**: Events use efficient ring buffers for user-space communication

## Troubleshooting

### BPF compilation fails

```bash
# Check for BTF support
ls -la /sys/kernel/btf/vmlinux

# If missing, install kernel-debuginfo
sudo dnf install -y kernel-debuginfo
```

### Permission denied

```bash
# Must run as root
sudo ./build.sh
```

### Program fails to load

```bash
# Check kernel version (needs 5.8+ for most features)
uname -r

# Check BPF support
sudo bpftool feature probe
```

## References

- [eBPF documentation](https://ebpf.io/)
- [libbpf documentation](https://libbpf.readthedocs.io/)
- [BPF CO-RE reference](https://nakryiko.com/posts/bpf-core-reference-guide/)
- [TC BPF documentation](https://man7.org/linux/man-pages/man8/tc-bpf.8.html)
