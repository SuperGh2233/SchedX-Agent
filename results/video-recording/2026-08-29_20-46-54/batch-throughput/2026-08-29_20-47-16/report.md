# SchedX-Agent Batch Throughput Report

| Phase | Events/s | P95 latency (ms) | Change vs interference | Background progress |
| --- | ---: | ---: | ---: | ---: |
| baseline | 18770.35 | 0.34 | 90.59% | 0.00% |
| interference | 9848.60 | 3.19 | 0.00% | 100.00% |
| schedx | 15588.02 | 0.34 | 58.28% | 33.30% |

The SchedX phase launches sysbench first, identifies the live batch workload,
and applies the throughput expert while CPU interference is active.

- Interference detected: True
- Result valid for claims: True