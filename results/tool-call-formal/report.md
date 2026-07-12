# Agent Tool-Call Resource-Control Experiment

This experiment compares the same Agent test tool call under full CPU
contention, first unmanaged and then managed by SchedX.

- Kernel: `6.15.11-schedx`
- Command: `python3 -m pytest -q`
- Repeats: 10
- CPU stress workers: 4
- Unmanaged mean duration: 0.2801 s
- Managed mean duration: 0.2320 s
- Tool-call latency reduction: 17.16%
- Unmanaged successful runs: 10
- Managed successful runs: 10
- Managed native sched_ext runs: 10
- Background CPU retention: 84.78%

The managed phase uses an ephemeral hierarchical cgroup, an explicit
Agent resource-intent hint, and the native weighted-vtime sched_ext scheduler.