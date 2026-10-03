# SchedX-Agent Redis Mixed Workload Report

This fixed-work benchmark rotates phase order across repeats to reduce order bias.
Each row reports a small-sample Student-t 95% confidence interval.

| Phase | Mean QPS | 95% CI | Mean P99 (ms) | 95% CI | Background retention | Valid |
| --- | ---: | --- | ---: | --- | ---: | --- |
| baseline | 124881.36 | [124864.03, 124898.69] | 1.02 | [0.88, 1.15] | n/a | True |
| interference | 81953.02 | [73835.51, 90070.54] | 3.12 | [2.99, 3.24] | 100.00% | True |
| schedx | 107380.95 | [103404.85, 111357.05] | 1.91 | [1.85, 1.97] | 61.25% | True |

## Result

- Interference QPS drop: 34.38%
- Agent QPS recovery: 31.03%
- Agent P99 reduction: 38.75%
- Fairness floor: 25.00%
- Result valid for claims: True

## Agent Evidence

The SchedX phase invokes the existing latency-first AgentLoop for Redis;
the saved action evidence includes classification, scx/cgroup execution, eBPF evidence, and cleanup.