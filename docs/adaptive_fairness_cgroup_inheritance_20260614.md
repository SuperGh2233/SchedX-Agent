# Adaptive Fairness And cgroup Policy Inheritance

Date: 2026-06-14

## cgroup Policy Inheritance

The BPF scheduler now resolves policy in this order:

```text
task policy -> default cgroup policy -> scheduler default
```

`tool-run` registers its ephemeral cgroup ID in the BPF map. Every child and
grandchild moved into that cgroup inherits the same class and weighted-vtime
policy without userspace PID discovery.

Verified result:

```text
cgroup policy present: true
task policy count: 0
policy scope: cgroup
native sched_ext: true
passed: true
```

## Adaptive Cross-Class Fairness

The previous strict class order could starve background work. The scheduler now
keeps per-CPU dispatch streaks and guarantees a background queue service
opportunity after a configurable interval.

The persistent daemon examines active workload classes and CPU PSI every five
seconds. It selects a service interval from:

| Mode | Background interval | Purpose |
| --- | ---: | --- |
| protected | 4096 | protect latency under high pressure |
| balanced | 2048 | normal mixed-workload balance |
| fair | 1024 | increase background progress under low pressure |
| uncontended | 512 | low-cost service when classes do not compete |

## Formal Performance Result

Environment: openEuler 24.03-LTS-SP3, Linux `6.15.11-schedx`, 5 repeats,
20 seconds per repeat, nginx with full-CPU background interference.

| Metric | Default scheduler | Adaptive SchedX |
| --- | ---: | ---: |
| Mean RPS | 72,391.29 | 136,017.10 |
| Mean P99 | 6.372 ms | 5.252 ms |
| Background CPU ticks | 14,082 | 2,639 |

Results:

- RPS gain: `87.89%`
- P99 reduction: `17.58%`
- background CPU retention: `18.74%`
- sched_ext rejected tasks: `0`

The old strict-priority experiment retained only `7.44%` background CPU.
Adaptive fairness increases background survival by roughly 2.5 times while
retaining positive throughput and latency improvements.

Evidence:

- `results/native-scx-adaptive-formal/`
- `results/adaptive-fairness/`
- `results/scx-daemon-concurrency-adaptive/`
