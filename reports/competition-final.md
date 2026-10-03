# SchedX-Agent Competition Report

## 1. Executive Summary

SchedX-Agent is an adaptive Linux resource-control Agent for mixed workloads.
It combines workload sensing, allowlisted expert routing, native sched_ext,
cgroup v2 enforcement, real SLO canaries, and automatic rollback.

- Demo artifact: `results\competition-demo`
- Demo status: `not available`
- Agent version: `not available`
- Kernel: `not available`
- Runtime mode: `not available`

## 2. Architecture

```mermaid
flowchart LR
  P[procfs / PSI / cgroup] --> C[Workload classifier]
  C --> R[Expert policy router]
  R --> B[Baseline SLO canary]
  B --> S[native sched_ext]
  B --> G[cgroup v2]
  S --> V[Candidate verifier]
  G --> V
  V -->|accepted| H[Policy outcome history]
  V -->|rejected| X[Automatic rollback]
```

## 3. Four-Way Nginx Ablation

| Phase | Mean RPS | Mean P99 (ms) | RPS gain | P99 reduction | Background retention | Valid |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| default | n/a | n/a | n/a | n/a | n/a | n/a |
| cgroup_only | n/a | n/a | n/a | n/a | n/a | n/a |
| scx_only | n/a | n/a | n/a | n/a | n/a | n/a |
| agent_combined | n/a | n/a | n/a | n/a | n/a | n/a |


The ablation isolates default Linux scheduling, cgroup-only control,
native scx-only control, and the combined adaptive Agent.
Only rows meeting the background-progress floor are valid performance claims.

## 4. Batch Throughput Scenario

| Phase | Mean events/s | P95 latency (ms) | Gain vs interference | Background progress | Valid |
| --- | ---: | ---: | ---: | ---: | --- |
| baseline | 16575.14 | 0.37 | 87.64% | 0.00% | n/a |
| interference | 8833.67 | 2.97 | 0.00% | 100.00% | n/a |
| schedx | 14535.26 | 0.37 | 64.54% | 27.19% | True |

### Batch Throughput

```mermaid
xychart-beta
  x-axis ["baseline", "interference", "schedx"]
  y-axis "Events/sec" 0 --> 18232.66
  bar [16575.14, 8833.67, 14535.26]
```

The SchedX phase identifies a live sysbench workload and applies the
throughput expert while CPU interference remains active.

## 5. Redis Latency Scenario

| Phase | Mean QPS | QPS 95% CI | Mean P99 (ms) | P99 95% CI | Background retention | Valid |
| --- | ---: | --- | ---: | --- | ---: | --- |
| baseline | 124881.36 | [124864.03, 124898.69] | 1.02 | [0.88, 1.15] | n/a | True |
| interference | 81953.02 | [73835.51, 90070.54] | 3.12 | [2.99, 3.24] | 100.00% | True |
| schedx | 107380.95 | [103404.85, 111357.05] | 1.91 | [1.85, 1.97] | 61.25% | True |

- QPS recovery vs interference: 31.03%
- P99 reduction vs interference: 38.75%

The fixed-work Redis experiment rotates phase order across five repeats
and reports small-sample Student-t 95% confidence intervals.

## 6. Canary And Rollback Evidence

### Accepted gate

- Final status: `n/a`
- Verdict: `n/a`
- RPS change: n/a
- P99 delta (negative is better): n/a
- Background retention: n/a
- Rollback evidence present: `False`

### Strict rollback gate

- Final status: `n/a`
- Verdict: `n/a`
- RPS change: n/a
- P99 delta (negative is better): n/a
- Background retention: n/a
- Rollback evidence present: `False`


## 7. Native sched_ext Scheduler Comparison

| Scheduler | Capability | Mean RPS | Mean P99 (ms) | RPS gain | P99 reduction | Background retention | Valid |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| default | none | 63804.61 | 7.49 | n/a | n/a | 100.00% | True |
| scx_simple | lifecycle_only | 86021.22 | 24.07 | 34.82% | -221.25% | 70.61% | True |
| scx_qmap | lifecycle_only | 5476.45 | 21.34 | -91.42% | -184.81% | 179.56% | True |
| scx_flatcg | lifecycle_only | 86154.63 | 23.84 | 35.03% | -218.18% | 70.32% | True |
| scx_agent | task_policy_and_fairness | 114224.98 | 2.99 | 79.02% | 60.04% | 30.48% | True |

The same repeated harness compares the default scheduler with allowlisted
upstream scx examples and SchedX's task-policy scheduler. Capability labels
prevent lifecycle-only schedulers from being presented as adaptive Agents.

## 8. Existing Native sched_ext Evidence

- RPS gain: 151.77%
- P99 reduction: 99.44%
- Background retention: n/a
- Valid for claims: `n/a`

## 9. Real eBPF Evidence

The Agent loop loads and attaches scheduler, network, resource, and security
BPF programs, updates allowlisted maps, collects counters for verification,
and removes pinned hooks through the same rollback path.

## 10. Optional LLM Evidence

- Source: `deepseek-v4`
- Mode: `latency_first`
- Target: `nginx`
- RPS gain: 84.96%
- P99 reduction: 55.16%

## 11. Reproduction

```bash
sudo bash scripts/run_competition_demo.sh
sudo bash scripts/run_competition_demo.sh --formal
sudo schedx benchmark scx-compare --schedulers scx_simple,scx_qmap,scx_flatcg,scx_agent --duration 20 --repeats 5
sudo schedx benchmark redis --duration 10 --repeats 5 --stress-cpu 4 --output results/redis-formal-final
python3 -m pytest -q
```

The short demo is presentation evidence. Formal claims should use
the repeated `--formal` run and retain all raw wrk/sysbench outputs.

## 12. Limitations

- Formal evidence covers nginx, Redis, and sysbench under CPU contention; it should not be generalized to every workload class.
- Same-host load generation can introduce client-side contention; an external load generator is preferable for final measurements.
- Network and security eBPF programs currently enforce bounded map policies and audit evidence; full production firewall or mandatory-access-control semantics remain future work.
- LLM policy planning is optional; safety and execution do not depend on model availability.