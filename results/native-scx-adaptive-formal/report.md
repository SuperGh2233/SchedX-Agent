# Native sched_ext Performance Comparison

- Kernel: `6.15.11-schedx`
- Duration per repeat: 20s
- Repeats: 5
- Default scheduler mean RPS: 72391.29
- Native sched_ext mean RPS: 136017.10
- RPS gain: 87.89%
- Default scheduler mean P99: 6.37 ms
- Native sched_ext mean P99: 5.25 ms
- P99 reduction: 17.58%
- Default background CPU ticks: 14082
- Native background CPU ticks: 2639
- Background CPU retention: 18.74%
- Native dispatch stats: `{"latency_dispatches": 983380, "batch_dispatches": 0, "background_dispatches": 1290, "default_dispatches": 639006, "total": 1623676}`