# Native sched_ext Progress

Date: 2026-06-14

## Implemented

- Replaced the legacy tracepoint-only `scx_agent` with a real
  `sched_ext_ops` BPF scheduler.
- Added latency, default/batch, and background dispatch queues.
- Added priority-ordered dispatch and weighted-vtime fairness within classes.
- Added a PID policy BPF map and userspace control protocol.
- Added a struct_ops loader with safe unregister on exit.
- Connected `ScxSkill` to the native scheduler execution path.
- Added a persistent systemd-managed scx daemon so concurrent Agents share one
  struct_ops owner through a Unix Socket policy API.

## Verified On openEuler VM

Kernel: `6.15.11-schedx`

```text
SchedX native sched_ext scheduler attached.
Default dispatches: 188
Latency dispatches: 0
Batch dispatches: 0
Background dispatches: 0
```

Kernel log:

```text
sched_ext: BPF scheduler "schedx_agent" enabled
sched_ext: BPF scheduler "schedx_agent" disabled (unregistered from user space)
```

`/sys/kernel/sched_ext/nr_rejected` remained `0`.

Dynamic PID policy verification:

```text
policy updated pid=9503 class=background weight=100
Default dispatches: 290
Background dispatches: 34
FINAL_STATE=disabled
REJECTED=0
```

Python controller end-to-end verification:

```text
start=True
state=enabled
set=True
background_dispatches=22
default_dispatches=120
policies={9974: {class_id: 3, weight: 100}}
stop=True
final_state=disabled
rejected=0
```

## Next Improvement

## Weighted-vtime And Formal Performance Experiment

Weighted-vtime now charges each task using its actual runtime:

```text
vtime += actual_runtime_ns * 1000 / policy_weight
```

Two same-class CPU-bound tasks pinned to one CPU with weights `100` and `1000`
produced an observed CPU-time ratio of approximately `1:2.74`. Weight changes
are effective, but local DSQ migration and minimum execution granularity mean
the observed ratio is not yet linear with the configured ratio.

Formal native sched_ext comparison:

- Kernel: `6.15.11-schedx`
- Repeats: 5
- Duration per repeat: 20 seconds
- Interference: stress-ng using all 4 CPUs
- Default scheduler mean RPS: `50479.17`
- Native sched_ext mean RPS: `142689.54`
- RPS gain: `182.67%`
- Default scheduler mean P99: `536.57 ms`
- Native sched_ext mean P99: `2.23 ms`
- P99 reduction: `99.58%`
- Background CPU retention: `7.44%`
- sched_ext rejected count: `0`

The original policy was intentionally latency-first and aggressively
deprioritized background work. This limitation is now addressed by adaptive
cross-class fairness and cgroup policy inheritance. The newer formal result
retains `18.74%` background CPU while improving RPS by `87.89%` and reducing
P99 by `17.58%`.

## Persistent Daemon Verification

Eight concurrent Agent tools registered eight policies in the same BPF map.
All eight completed in native daemon mode, policies returned to zero afterward,
and the scheduler remained enabled. A deliberately stale PID policy was
automatically reaped. The kernel reported `nr_rejected=0`.
