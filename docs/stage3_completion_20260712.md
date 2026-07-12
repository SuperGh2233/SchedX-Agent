# SchedX-Agent Stage 3 Completion Notes

Updated: 2026-07-12

## Completed Items

This round completed the three requested improvements:

1. One-click competition demo script.
2. LLM policy performance comparison experiment.
3. sched_ext cgroup metric cleanup and cleaner daemon telemetry.

## 1. One-Click Demo

Added `scripts/run_competition_demo.sh`.

The script chains the main competition proof points:

- SchedX environment and native `sched_ext` status.
- DeepSeek V4 policy planning.
- Persistent `schedx-scx-daemon` status.
- zero-value cgroup metric cleanup.
- Agent tool call under daemon-owned native `sched_ext`.
- cgroup-level policy inheritance verification.
- closed-loop background fairness convergence verification.
- final daemon status.

Latest VM run:

- Output: `results/competition-demo/`
- Kernel: `6.15.11-schedx`
- `sched_ext` final state: `enabled`
- current scheduler: `schedx_agent`
- daemon: running
- `nr_rejected`: `0`
- cgroup inheritance: passed
- closed-loop convergence: passed
- final background runtime share: about `17.51%`

Demo command:

```bash
bash scripts/run_competition_demo.sh results/competition-demo
```

## 2. DeepSeek V4 Policy Comparison

Added `scripts/run_llm_policy_experiment.py`.

The experiment compares:

- default Linux scheduling under nginx + stress-ng interference.
- deterministic rule-based SchedX policy.
- DeepSeek V4-guided SchedX policy.

The LLM decision is now taken after the stress workload starts, so the saved result proves that the model saw both latency-sensitive nginx tasks and background stress-ng tasks.

Latest formal VM run:

- Output: `results/llm-policy-comparison/`
- Duration: `10s`
- Repeats: `3`
- LLM source: `deepseek-v4`
- LLM classified latency tasks: `5`
- LLM classified background tasks: `5`

Performance summary:

| Mode | Mean RPS | Mean P99 | RPS gain vs default | P99 reduction vs default | Background CPU retention |
| --- | ---: | ---: | ---: | ---: | ---: |
| Rule SchedX | `124381.39` | `2.78 ms` | `84.03%` | `55.21%` | `31.31%` |
| DeepSeek SchedX | `125009.07` | `2.78 ms` | `84.96%` | `55.16%` | `31.20%` |

Interpretation:

- Native SchedX remains the main performance source.
- DeepSeek V4 correctly selected `latency_first / nginx` under CPU interference.
- LLM-guided policy achieved similar latency improvement to deterministic rules and slightly higher throughput in this run.
- The LLM path is now defensible as a policy-planning layer rather than just a dry-run demo.

Experiment command:

```bash
python3 scripts/run_llm_policy_experiment.py --duration 10 --repeats 3 --output results/llm-policy-comparison
```

## 3. Metric Cleanup And Daemon Telemetry

Updated `schedx/scx_daemon.py`.

Implemented:

- daemon action: `cleanup_metrics`
- CLI action: `schedx scx-daemon cleanup-metrics`
- automatic cleanup of orphan zero-valued cgroup metrics.
- `reaped_metrics` counter in daemon status.
- telemetry target refresh when `set_target` changes the background-share target.

This keeps the demo output clean and avoids stale BPF map metrics from making the project look unstable.

Validation:

- Local tests: `63 passed, 6 skipped`
- VM tests: `69 passed`
- VM daemon status after demo: `sched_ext.enabled`, scheduler `schedx_agent`, daemon running

## Current Competition Position

The project now has a complete end-to-end story:

```text
Agent/tool workload
  -> workload classification
  -> DeepSeek/rule policy decision
  -> persistent sched_ext daemon
  -> native sched_ext weighted-vtime dispatch
  -> cgroup policy inheritance
  -> per-cgroup metrics
  -> closed-loop fairness adjustment
  -> reportable benchmark evidence
```

Compared with the previous stage, the project is stronger in three scoring areas:

- Performance: native sched_ext formal experiments plus the new LLM comparison show stable throughput and P99 gains.
- Innovation: DeepSeek V4 participates in runtime policy planning, while the kernel scheduler enforces the result.
- Completeness: one-click demo, daemon lifecycle, metric cleanup, inheritance verification, and closed-loop validation are now tied together.

## Remaining Optimization Space

Recommended next improvements:

- Add a multi-agent concurrent LLM scenario, not just nginx + stress-ng.
- Add Redis or compile workload into the LLM comparison matrix.
- Generate a final competition report from `results/` automatically.
- Add charts for RPS, P99, background CPU retention, and convergence intervals.
- Package environment setup and kernel requirements into a judge-friendly checklist.

