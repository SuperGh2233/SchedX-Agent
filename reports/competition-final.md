# SchedX-Agent Competition Summary

This report is generated from local experiment artifacts and is intended
as a compact judging/defense summary.

## Core Story

SchedX-Agent turns Agent/tool workloads into validated scheduling intent,
uses a persistent daemon to own the native sched_ext scheduler, applies
weighted-vtime cgroup policies, collects per-cgroup metrics, and closes
the loop by tuning fairness from observed runtime share.

```text
Agent workload -> DeepSeek/rule policy -> schedx-scx-daemon
  -> native sched_ext weighted vtime -> cgroup metrics
  -> closed-loop fairness -> reproducible reports
```

## Performance Evidence

| Evidence | Metric | Result |
| --- | --- | ---: |
| Native sched_ext formal | RPS gain | 88.29% |
| Native sched_ext formal | P99 reduction | 28.16% |
| Native sched_ext formal | Background retention | 17.29% |
| DeepSeek policy | RPS gain | 84.96% |
| DeepSeek policy | P99 reduction | 55.16% |
| Rule policy | RPS gain | 84.03% |
| Rule policy | P99 reduction | 55.21% |
| Agent tool-call control | Latency reduction | 17.16% |
| Agent tool-call control | Native sched_ext runs | 10 |

## Capability Evidence

| Capability | Evidence | Status |
| --- | --- | --- |
| Native sched_ext mounted | `schedx_agent` / `enabled` | passed |
| Persistent daemon | final status from one-click demo | passed |
| cgroup policy inheritance | `results/competition-demo/cgroup-inheritance.json` | passed |
| Closed-loop fairness | `results/competition-demo/closed-loop-convergence.json` | passed |
| Multi-Agent daemon sharing | `6/6` tools | passed |
| Dead policy cleanup | `results/scx-daemon-concurrency/summary.json` | passed |
| LLM policy planning | source `deepseek-v4` | passed |

## LLM Decision

- Source: `deepseek-v4`
- Mode: `latency_first`
- Target: `nginx`
- Reason: DeepSeek V4 proposal: nginx is latency-sensitive, stress-ng are background noise. Prioritize nginx with high weight and limit background CPU.

## Score-Oriented Assessment

- Performance: strong, with repeated native sched_ext and LLM-policy gains.
- Completeness: strong, based on demo, inheritance, and convergence artifacts.
- Innovation: strong, combining DeepSeek planning, daemonized sched_ext ownership, cgroup inheritance, and closed-loop metrics.
- Overall readiness estimate: high.

## Key Artifact Paths

- `results/competition-demo/`
- `results/llm-policy-comparison/`
- `results/multi-agent-llm/`
- `results/native-scx-metrics-formal/`
- `docs/stage3_completion_20260712.md`