# SchedX-Agent Competition Evidence Matrix

This index maps every explicit competition requirement to executable code and
real openEuler SP4 evidence. It is the quickest judge-facing entry point when a
claim needs to be verified.

## Final-round iteration evidence (2026-10-04)

The current implementation continues the same repository history. See
[final-round improvements](final-round-improvements.md) for the substantive
code changes and their evidence. The team has confirmed the preliminary
submission as `b0591a10785b74cf980c8f0b4ed703a9a95e1aa2` (2026-07-30).
Its Git object and ancestry to the current branch have been checked.
`58903fe` remains the October optimization starting point, not the preliminary
submission. The preliminary suite collects 141 tests; the current suite passes 241.

The October paired experiments compare `59c1ca4` with `58903fe`; they show
nginx/Redis P99 reductions of 49.48%/30.67% and a 2.26% batch throughput cost
on the verified four-vCPU VM. The current implementation has 241 passing
tests and additional monitor-state and task-idempotence verification. Keep
these version-specific results separate from the earlier formal results below.
An exact preliminary-versus-final paired performance comparison is still pending.

Sources: [kernel/performance acceptance](../reports/optimization-20261004/report.md)
and [continuous monitoring followup](../reports/optimization-continuous-20261004/report.md).

| Competition requirement | Implemented capability | Code evidence | Runtime evidence | Verification command |
| --- | --- | --- | --- | --- |
| Standard tools and Skills | Unified `Skill`/`SkillResult`; Probe, Analyze, LLM Policy, Policy, Canary, eBPF, scx, Act, Verify, Rollback and Report phases | `schedx/agent/loop.py`, `schedx/skills/` | `results/video-recording/2026-08-29_20-46-54/agent-trace.json` | `bash scripts/demo_evidence.sh` |
| Workload sensing | procfs process metrics, scheduler fields, PSI, cgroup statistics, CPU topology and rule reasons | `schedx/probes/`, `schedx/policies/classifier.py` | classification embedded in the video and Redis action evidence | `schedx classify --top 50` |
| sched_ext CPU policy | Native SP4 sched_ext, task classes, weights, fairness and rollback | `scx/`, `schedx/controllers/scx_controller.py` | `results/scx-compare-formal/2026-08-29_20-14-31/summary.json` | `schedx status` |
| Integrated scx scheduler | Custom `scx_agent` with class enqueue and actual runtime metrics | `scx/scx_agent.bpf.c`, `scx/scx_agent_user.c` | legacy counter total 1,679,433 in the earlier formal comparison (enqueue events, not actual dispatches) | `schedx benchmark scx-compare ...` |
| eBPF extension hooks | Real scheduler trace, cgroup network, resource and BPF-LSM security hooks; structured maps and counters | `ebpf/`, `schedx/controllers/ebpf_controller.py`, `schedx/skills/ebpf_skill.py` | four attached hook families and nine policies in the Agent trace | `make -C ebpf clean all` |
| Safe policy execution | Structured Action schema, target allowlist, explicit dry-run, Canary gate and multi-plane rollback | `schedx/agent/executor.py`, `schedx/skills/verify_skill.py`, `schedx/skills/rollback_skill.py` | strict gate removes cgroup state, eight scx policies and four eBPF hook families | `schedx rollback` |
| Nginx performance | Four-way default/cgroup/scx/Agent ablation | `schedx/benchmark/ablation.py` | formal and video-recording summaries | `schedx benchmark nginx-ablation ...` |
| Redis generalization | Rotated baseline/interference/Agent experiment with Student-t 95% CI | `schedx/benchmark/redis.py` | `results/redis-formal-final/2026-08-30_01-15-52/summary.json` | `bash scripts/run_redis_experiment.sh` |
| Batch performance | sysbench baseline/interference/Agent experiment with fairness gate | `schedx/benchmark/batch.py` | `results/batch-formal-final/` | `schedx benchmark batch-throughput ...` |
| Scheduler comparison | Same harness for default, scx_simple, scx_qmap, scx_flatcg and scx_agent | `schedx/benchmark/scx_comparison.py` | five 20-second repeats with raw output and cleanup | `schedx benchmark scx-compare ...` |
| Reproducible environment | SP4 kernel build instructions, dependency scripts, timestamped raw data, CSV/JSON/Markdown reports | `kernel/`, `scripts/`, `docs/experiment.md` | openEuler kernel `6.6.0-159.4.3.154.oe2403sp4.schedx1` | `python3 -m pytest -q` |

## Verified Formal Results

| Scenario | Interference effect | SchedX result | Fairness |
| --- | ---: | ---: | ---: |
| Nginx scheduler comparison | default P99 7.492 ms | RPS +79.02%, P99 -60.04% | background 30.48% |
| Redis mixed workload | QPS -34.38% | QPS +31.03%, P99 -38.75% vs interference | background 61.25% |
| Sysbench batch workload | throughput -46.71% | throughput +64.54%, P95 2.97 to 0.37 ms | background 27.19% |

All quoted performance claims pass the configured 25% background-progress
floor. Negative and invalid runs remain in the raw result directories and are
not rewritten as improvements.

## Honest Boundaries

- The formal workloads are CPU-contention scenarios on a four-vCPU VMware VM.
- Client and server currently share the same VM; an external load generator is
  the preferred next validation step.
- Network and security eBPF hooks implement bounded policy and audit semantics,
  not a production firewall or mandatory-access-control product.
- DeepSeek proposes structured policy only. The deterministic safety path can
  run without network or model availability.
