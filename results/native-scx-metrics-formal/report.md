# Native sched_ext Performance Comparison

- Kernel: `6.15.11-schedx`
- Duration per repeat: 20s
- Repeats: 5
- Default scheduler mean RPS: 72479.27
- Native sched_ext mean RPS: 136471.39
- RPS gain: 88.29%
- Default scheduler mean P99: 6.44 ms
- Native sched_ext mean P99: 4.63 ms
- P99 reduction: 28.16%
- Default background CPU ticks: 14190
- Native background CPU ticks: 2453
- Background CPU retention: 17.29%
- Native dispatch stats: `{"latency_dispatches": 996684, "batch_dispatches": 0, "background_dispatches": 1196, "default_dispatches": 663192, "total": 1661072}`