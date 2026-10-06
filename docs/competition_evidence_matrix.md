# SchedX-Agent Competition Evidence Matrix

This index maps every explicit competition requirement to executable code and
real openEuler SP4 evidence. It is the quickest judge-facing entry point when a
claim needs to be verified.

## Final-round iteration evidence (2026-10-06)

The team-confirmed preliminary submission is `b0591a10785b74cf980c8f0b4ed703a9a95e1aa2` (2026-07-30), an ancestor of the current optimization branch. The frozen production implementation is `5ac43a8102200cbc926613c580ff331296cf9db7`; Linux passes all 452 tests, while macOS passes 451 and skips one Linux-only pidfd check. The preliminary suite collects 141 tests; test count alone is not a claim of substantive improvement.

The latest new tool admission path passes five rotated off/fixed/adaptive comparisons, independent-process recovery, 30-minute and two-hour validation. The two-hour run completes 55,660 tool calls and 60 recovery checks. Fixed admission reduces interactive batch tail time by 65.7269%, with successful tool throughput reduced by 2.0912%. This compares the same current tool entry point with admission off/on; six interactive samples per batch approximate its maximum. It is not service P99 or preliminary whole-Agent improvement. Adaptive superiority over fixed is not established.

Full-Agent quality gates, declared process scope, recovery and bounded observation were validated at the preceding `47440e2`: 46 rounds, including 31 without control changes. Formal preliminary component comparisons have been completed; they do not establish significant performance improvement. A static configuration fails the batch background-progress gate; a declared shared runtime-feedback adapter passes regression gates without significant gain. These experiments are not unmodified preliminary whole-Agent comparisons.

Earlier October `59c1ca4` versus `58903fe` nginx/Redis P99 reductions of 49.48%/30.67% remain historical, version-specific evidence. `58903fe` is the October starting point, not the official preliminary submission. Keep these separate from the earlier formal results below and the latest tool-admission measurements.

Sources: [substantive improvements](final-round-improvements.md), [latest admission acceptance](../reports/optimization-admission-20261006/report.md), [full Agent and preliminary comparisons](../reports/optimization-acceptance-20261005/report.md), [earlier October performance](../reports/optimization-20261004/report.md).

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
| Shared tool admission | Cross-process bounded queue, priority/aging, inherited lease, submission deadline, PSI control and bounded cgroup topology coordination | `schedx/admission.py`, `schedx/tool_runner.py` | `reports/optimization-admission-20261006/report.md`: five paired comparisons, 55,660 calls / 60 recoveries in two hours; final cleanup and original-file audit pass | `python3 scripts/verify_tool_admission.py --help` |

## Earlier Verified Formal Results

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
