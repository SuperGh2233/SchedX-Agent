# Native sched_ext Performance Comparison

- Kernel: `6.15.11-schedx`
- Duration per repeat: 10s
- Repeats: 3
- Default scheduler mean RPS: 72415.53
- Native sched_ext mean RPS: 127919.99
- RPS gain: 76.65%
- Default scheduler mean P99: 6.21 ms
- Native sched_ext mean P99: 7.77 ms
- P99 reduction: -25.19%
- Default background CPU ticks: 4215
- Native background CPU ticks: 1465
- Background CPU retention: 34.76%
- Native dispatch stats: `{"latency_dispatches": 287280, "batch_dispatches": 0, "background_dispatches": 717, "default_dispatches": 183630, "total": 471627}`