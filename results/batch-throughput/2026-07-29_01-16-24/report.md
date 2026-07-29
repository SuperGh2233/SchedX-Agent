# SchedX-Agent Batch Throughput Report

| Phase | Events/s | P95 latency (ms) | Change vs interference | Background progress |
| --- | ---: | ---: | ---: | ---: |
| baseline | 16575.14 | 0.37 | 87.64% | 0.00% |
| interference | 8833.67 | 2.97 | 0.00% | 100.00% |
| schedx | 14535.26 | 0.37 | 64.54% | 27.19% |

The SchedX phase launches sysbench first, identifies the live batch workload,
and applies the throughput expert while CPU interference is active.

- Interference detected: True
- Result valid for claims: True