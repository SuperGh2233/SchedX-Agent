# SchedX-Agent Design

SchedX-Agent uses a deterministic control loop:

1. Probe Linux state from procfs, PSI, cgroup v2, and later eBPF hooks.
2. Classify processes into `latency_sensitive`, `batch_compute`, `background_noise`, or `mixed`.
3. Generate structured actions with a policy planner.
4. Execute only whitelisted actions through scx and cgroup controllers.
5. Record rollback metadata before mutating cgroup files.
6. Verify with benchmark wrappers and generate reports.

Phase 1 intentionally uses cgroup-only fallback when sched_ext is unavailable. The scx controller already exposes detection and lifecycle interfaces so a real scheduler loader can be added without changing the CLI contract.

