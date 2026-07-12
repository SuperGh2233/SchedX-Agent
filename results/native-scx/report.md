# Native sched_ext Performance Comparison

- Kernel: `6.15.11-schedx`
- Duration per repeat: 10s
- Repeats: 3
- Default scheduler mean RPS: 56948.44
- Native sched_ext mean RPS: 143379.21
- RPS gain: 151.77%
- Default scheduler mean P99: 338.75 ms
- Native sched_ext mean P99: 1.91 ms
- P99 reduction: 99.44%
- Native dispatch stats: `{"latency_dispatches": 292744, "batch_dispatches": 0, "background_dispatches": 285, "default_dispatches": 214390, "total": 507419}`