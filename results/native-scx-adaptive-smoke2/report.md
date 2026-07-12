# Native sched_ext Performance Comparison

- Kernel: `6.15.11-schedx`
- Duration per repeat: 10s
- Repeats: 3
- Default scheduler mean RPS: 72865.23
- Native sched_ext mean RPS: 69397.81
- RPS gain: -4.76%
- Default scheduler mean P99: 6.43 ms
- Native sched_ext mean P99: 13.68 ms
- P99 reduction: -112.86%
- Default background CPU ticks: 4263
- Native background CPU ticks: 6219
- Background CPU retention: 145.88%
- Native dispatch stats: `{"latency_dispatches": 245787, "batch_dispatches": 0, "background_dispatches": 3036, "default_dispatches": 115750, "total": 364573}`