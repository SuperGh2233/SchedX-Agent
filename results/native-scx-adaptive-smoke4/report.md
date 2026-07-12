# Native sched_ext Performance Comparison

- Kernel: `6.15.11-schedx`
- Duration per repeat: 10s
- Repeats: 3
- Default scheduler mean RPS: 72939.64
- Native sched_ext mean RPS: 132561.96
- RPS gain: 81.74%
- Default scheduler mean P99: 6.16 ms
- Native sched_ext mean P99: 4.71 ms
- P99 reduction: 23.55%
- Default background CPU ticks: 4141
- Native background CPU ticks: 1141
- Background CPU retention: 27.55%
- Native dispatch stats: `{"latency_dispatches": 285247, "batch_dispatches": 0, "background_dispatches": 558, "default_dispatches": 190121, "total": 475926}`