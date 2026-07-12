# cgroup Scheduler Metrics And Closed-Loop Tuning

Date: 2026-06-14

## BPF Metrics

The native scheduler collects metrics only for registered managed cgroups:

- `enqueues`: number of queue insertions;
- `runs`: number of times a task begins running;
- `runtime_ns`: cumulative on-CPU runtime;
- `wait_ns`: cumulative enqueue-to-running wait time.

Metrics are keyed by cgroup ID, so they naturally aggregate complete Agent tool
process trees. They are available through `schedx scx-daemon metrics` and are
stored in each `tool-run` result.

## Closed Loop

Every five seconds the persistent daemon calculates runtime deltas by workload
class. For mixed latency and background workloads it compares the observed
background runtime share with the configurable default target of `12%–25%`:

```text
share below target -> halve background service interval
share above target -> double background service interval
share inside target -> retain current interval
```

Adjustments are limited to one step per sampling window and bounded between
`512` and `4096`, preventing abrupt policy oscillation.

## openEuler Verification

Two competing cgroups produced independently observable metrics:

```text
latency cgroup:    runtime_ns=39,828,535,354 wait_ns=8,305,844,443
background cgroup: runtime_ns=8,275,777,754  wait_ns=39,024,058,306
background runtime share=17.85%
decision=runtime_share_target
```

A controlled convergence experiment temporarily requested a `30%–40%`
background share. Starting from interval `4096`, the controller observed a low
runtime share and adjusted:

```text
4096 -> 2048 -> 1024 -> 512
```

The target was restored to `12%–25%` after verification. The daemon remained
active, sched_ext remained enabled, and `nr_rejected=0`.

Evidence:

- `results/adaptive-fairness-closed-loop/`
- `results/closed-loop-convergence/`
- `results/native-scx-metrics-formal/`

## Performance Regression Check

The final 5-repeat, 20-second native scheduler comparison after enabling BPF
cgroup metrics produced:

```text
RPS gain: 88.29%
P99 reduction: 28.16%
background CPU retention: 17.29%
nr_rejected: 0
```

The result confirms that managed-cgroup metric collection and closed-loop
control retain the scheduler's positive throughput and latency improvements.
