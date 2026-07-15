# scx_agent - SchedX Agent BPF Scheduler

A custom sched_ext scheduler for SchedX-Agent that classifies tasks by workload type and dispatches them with different priorities.

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
│                 scx_agent_user (User-space)                  │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  BPF Map Management │ Policy Updates │ Statistics    │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                scx_agent.bpf.o (Kernel BPF)                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │  select  │→│ enqueue  │→│ dispatch │→│ consume  │    │
│  │   cpu    │  │          │  │          │  │          │    │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘    │
│                                                             │
│  DSQs:                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │ Latency  │  │  Global  │  │  Batch   │  │  Bgnd    │    │
│  │ (high)   │  │ (normal) │  │ (normal) │  │  (low)   │    │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘    │
└─────────────────────────────────────────────────────────────┘
```

## Workload Classification

| Class | ID | Priority | Default Slice | Weight | Description |
|-------|-----|----------|---------------|--------|-------------|
| Unknown | 0 | Normal | 100% | 1000 | Default behavior |
| Latency | 1 | High | 50% cap | 10000 | nginx, redis, envoy |
| Batch | 2 | Normal | 100% | 1000 | gcc, make, sysbench |
| Background | 3 | Low | 10% | 100 | stress-ng, openssl |

## BPF Maps

### task_policy_map (Hash)
- **Key**: PID (__u32)
- **Value**: struct schedx_policy { class_id, weight }
- **Purpose**: Per-task scheduling policy

### cgroup_policy_map (Hash)
- **Key**: cgroup_id (__u64)
- **Value**: struct schedx_policy { class_id, weight }
- **Purpose**: Per-cgroup scheduling policy

### stats_map (Per-CPU Array)
- **Key**: 0 (single entry)
- **Value**: struct schedx_stats { latency/batch/bg/default dispatches }
- **Purpose**: Dispatch statistics

## Building

### Prerequisites

```bash
# openEuler 24.03-LTS-SP3
sudo dnf install -y gcc clang llvm llvm-tools make cmake git
sudo dnf install -y kernel-devel kernel-headers
sudo dnf install -y libbpf-devel libbpf elfutils-libelf-devel zlib-devel
sudo dnf install -y kernel-tools  # for bpftool
```

### Build

```bash
cd scx
make          # Build both BPF and user-space
make bpf      # Build only BPF program
make user     # Build only user-space loader
```

### Install

```bash
sudo make install  # Install to /usr/local/bin/scx_agent
```

## Usage

### Interactive Mode

```bash
sudo ./output/scx_agent
schedx> set task 1234 1 10000    # Set pid 1234 as latency-sensitive
schedx> set cgroup 42 3 100      # Set cgroup 42 as background
schedx> stats                    # Show dispatch statistics
schedx> dump                     # Dump all policies
schedx> quit                     # Exit
```

### From SchedX-Agent

The Python `ScxController` manages the scheduler automatically:

```python
from schedx.controllers.scx_controller import ScxController

scx = ScxController()
if scx.is_available():
    scx.start_scheduler("scx_agent")
    scx.set_task_policy(pid=1234, class_id=1, weight=10000)
    scx.set_cgroup_policy(cgroup_id=42, class_id=3, weight=100)
    stats = scx.get_stats()
    scx.stop_scheduler()
```

### Dry-Run Mode

```bash
./output/scx_agent --dry-run  # Test without loading BPF
```

## Integration with SchedX-Agent

1. **Probe**: SchedX-Agent collects process information from /proc
2. **Classify**: Workloads are classified into latency/batch/background
3. **Policy**: PolicyPlanner generates actions based on classification
4. **Act**: ScxController loads scx_agent and updates BPF maps
5. **Verify**: Monitor dispatch statistics and adjust policies

## Performance Tuning

### Time Slices

Within a class, task slices scale with policy weight around the default weight
of 1000. Slices are clamped to 10%-400% of `SCX_SLICE_DFL`; latency-class
slices have an additional 50% cap to protect tail latency. Weighted slices
complement vtime ordering on kernels where a task may already be prefetched to
a CPU-local DSQ before the current task is re-enqueued.

### Weights

Weights are aligned with cgroup cpu.weight (1-10000):
- Higher weight = more CPU time
- Latency tasks get 10000 (maximum)
- Background tasks get 100 (minimum)

Verify same-class weight enforcement on the target kernel with:

```bash
python scripts/verify_scx_weight.py
```

The verifier compares weights 100 and 1000 on one CPU and fails unless the
observed CPU-time ratio is at least 5:1.

## Troubleshooting

### sched_ext not available

```bash
# Check kernel support
grep CONFIG_SCHED_CLASS_EXT /boot/config-$(uname -r)

# Check if loaded
ls -la /sys/kernel/sched_ext/
```

### Permission denied

```bash
# Must run as root
sudo ./output/scx_agent
```

### Build fails

```bash
# Check dependencies
./build.sh --check

# Install missing deps
sudo ./build.sh --deps
```

## References

- [sched_ext documentation](https://docs.kernel.org/scheduler/sched-ext.html)
- [libbpf documentation](https://libbpf.readthedocs.io/)
- [scx examples](https://github.com/sched-ext/scx)

