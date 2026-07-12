# Agent Tool-Call Resource-Control Experiment

This experiment compares the same Agent test tool call under full CPU
contention, first unmanaged and then managed by SchedX.

- Kernel: `6.15.11-schedx`
- Command: `python3 -m pytest -q`
- Repeats: 3
- CPU stress workers: 4
- Unmanaged mean duration: 0.2893 s
- Managed mean duration: 0.2150 s
- Tool-call latency reduction: 25.70%
- Unmanaged successful runs: 3
- Managed successful runs: 3
- Managed native sched_ext runs: 3
- Background CPU retention: 73.67%

The managed phase uses an ephemeral hierarchical cgroup, an explicit
Agent resource-intent hint, and the native weighted-vtime sched_ext scheduler.