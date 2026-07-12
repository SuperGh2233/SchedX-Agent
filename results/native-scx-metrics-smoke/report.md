# Native sched_ext Performance Comparison

- Kernel: `6.15.11-schedx`
- Duration per repeat: 10s
- Repeats: 3
- Default scheduler mean RPS: 74688.43
- Native sched_ext mean RPS: 140440.84
- RPS gain: 88.04%
- Default scheduler mean P99: 6.53 ms
- Native sched_ext mean P99: 4.46 ms
- P99 reduction: 31.61%
- Default background CPU ticks: 4206
- Native background CPU ticks: 770
- Background CPU retention: 18.31%
- Native dispatch stats: `{"latency_dispatches": 299892, "batch_dispatches": 0, "background_dispatches": 380, "default_dispatches": 213889, "total": 514161}`