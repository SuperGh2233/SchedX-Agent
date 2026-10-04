# SchedX Native sched_ext Scheduler

`scx_agent` is the native kernel scheduling backend for SchedX-Agent. It is a
real `sched_ext_ops` BPF scheduler and is not an emulation layer.

## Design

The scheduler exposes four policy classes:

| Class | ID | Default weight | Purpose |
| --- | ---: | ---: | --- |
| unknown | 0 | 1000 | Unclassified tasks |
| latency | 1 | 10000 | nginx, Redis and online services |
| batch | 2 | 1000 | compilers and throughput jobs |
| background | 3 | 100 | stress and best-effort work |

Userspace updates two BPF maps:

- `task_policy_map`: PID to class and weight.
- `cgroup_policy_map`: cgroup ID to class and weight, inherited by descendants.

Each class has its own DSQ. Latency and ordinary tasks use round-robin order
with capped, weight-scaled slices so sleeping peers cannot repeatedly overtake
a queued task. Batch and background tasks retain weighted virtual-time
ordering with weight-scaled idle credit and execution-time charging.
Cross-class fairness gives ordinary and batch queues separate service
opportunities every `32` dispatches. The initial background interval is `64`;
the daemon can tune it from observed background runtime share.

The scheduler also exports:

- per-class enqueue counters (the legacy `stats` protocol) and actual run,
  runtime, wait and maximum-wait metrics (`class_metrics`);
- per-cgroup enqueue, run, runtime and wait metrics;
- dynamic task/cgroup policy removal;
- fairness updates without reloading struct_ops.

## Kernel Requirement

The stock openEuler 24.03 LTS SP4 binary kernel does not enable
`CONFIG_SCHED_CLASS_EXT`. The source RPM does contain sched_ext, so use the
config-only rebuild documented at:

```text
kernel/openEuler-24.03-LTS-SP4/README.md
```

The validated kernel is
`6.6.0-159.4.3.154.oe2403sp4.schedx1`.

## Build

`KERNEL_SCX_DIR` must point to the `tools/sched_ext` directory from the exact
kernel source used for the running experiment. The default matches the
validated VM build tree and can be overridden:

```bash
cd scx
KERNEL_SCX_DIR=/root/kernel-build/kernel/tools/sched_ext ./build.sh --check
KERNEL_SCX_DIR=/root/kernel-build/kernel/tools/sched_ext ./build.sh
sudo install -m 0755 output/scx_agent /usr/local/bin/scx_agent
```

The build copies `scx_agent.bpf.c`, `scx_agent_user.c` and the shared header
into the kernel sched_ext toolchain so libbpf compatibility headers and the
generated skeleton match the tested kernel ABI.

## Interactive Protocol

```text
set task <pid> <class_id> <weight>
set cgroup <cgroup_id> <class_id> <weight>
remove task <pid>
remove cgroup <cgroup_id>
set fairness <background_interval> <default_interval>
stats
metrics
class_metrics
dump
quit
```

Example:

```bash
sudo scx_agent
schedx> set task 1234 1 10000
schedx> set task 5678 3 100
schedx> set fairness 64 32
schedx> stats
schedx> quit
```

For normal operation, use the persistent daemon instead of driving the
interactive protocol directly:

```bash
sudo systemctl enable --now schedx-scx-daemon
schedx scx-daemon status
```

## Verification

```bash
sudo python3 scripts/verify_scx_weight.py
sudo python3 scripts/verify_scx_cgroup_inheritance.py
sudo python3 scripts/verify_adaptive_fairness.py
```

The formal comparison additionally rejects a performance claim when
background CPU retention is below 25%:

```bash
sudo python3 scripts/run_native_scx_experiment.py
```

On systems without sched_ext, SchedX skips this backend and continues in
cgroup-only fallback mode.
