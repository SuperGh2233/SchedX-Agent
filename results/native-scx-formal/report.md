# Native sched_ext Performance Comparison

- Kernel: `6.15.11-schedx`
- Duration per repeat: 20s
- Repeats: 5
- Default scheduler mean RPS: 50479.17
- Native sched_ext mean RPS: 142689.54
- RPS gain: 182.67%
- Default scheduler mean P99: 536.57 ms
- Native sched_ext mean P99: 2.23 ms
- P99 reduction: 99.58%
- Default background CPU ticks: 20205
- Native background CPU ticks: 1503
- Background CPU retention: 7.44%
- Native dispatch stats: `{"latency_dispatches": 992021, "batch_dispatches": 0, "background_dispatches": 738, "default_dispatches": 681399, "total": 1674158}`