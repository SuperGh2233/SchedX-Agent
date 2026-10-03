# SchedX-Agent Nginx Ablation Report

| Phase | Mean RPS | Mean P99 (ms) | RPS gain | P99 reduction | Background retention | Valid |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| default | 60156.46 | 2.27 | 0.00% | 0.00% | 100.00% | True |
| cgroup_only | 78703.17 | 1.12 | 30.83% | 50.66% | 25.20% | True |
| scx_only | 65972.53 | 0.25 | 9.67% | 88.94% | 98.21% | True |
| agent_combined | 65154.59 | 0.26 | 8.31% | 88.50% | 96.59% | True |

The four phases isolate the contribution of cgroup v2, native sched_ext, and the combined Agent.
Performance claims are valid only when the configured background-progress floor is met.