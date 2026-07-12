# Native sched_ext Performance Comparison

- Kernel: `6.15.11-schedx`
- Duration per repeat: 10s
- Repeats: 3
- Default scheduler mean RPS: 74181.97
- Native sched_ext mean RPS: 9653.29
- RPS gain: -86.99%
- Default scheduler mean P99: 7.72 ms
- Native sched_ext mean P99: 17.99 ms
- P99 reduction: -133.03%
- Default background CPU ticks: 4201
- Native background CPU ticks: 11243
- Background CPU retention: 267.63%
- Native dispatch stats: `{"latency_dispatches": 23069, "batch_dispatches": 0, "background_dispatches": 5489, "default_dispatches": 20815, "total": 49373}`