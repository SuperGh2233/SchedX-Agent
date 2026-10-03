# Reproducible Competition Experiments

## One-Click Demo

```bash
sudo bash scripts/run_competition_demo.sh
sudo bash scripts/run_competition_demo.sh --formal
```

The short profile is intended for a live presentation. The formal profile uses
three 20-second repeats and should be used for final claims. Both profiles save
raw output, summaries, canary decisions, rollback evidence, and cleanup state
under `results/competition-demo/<timestamp>/`.

## Four-Way Nginx Ablation

```bash
sudo schedx benchmark nginx-ablation \
  --duration 20 --connections 64 --threads 4 \
  --repeats 3 --stress-cpu 4 \
  --min-background-retention 25 \
  --output results/nginx-ablation
```

The phases are:

1. `default`: Linux default scheduler with stress-ng interference.
2. `cgroup_only`: Agent classification and cgroup v2 enforcement, sched_ext disabled.
3. `scx_only`: native `scx_agent` policies without cgroup controls.
4. `agent_combined`: automatic Agent decision with native scx and cgroup v2.

Every phase records RPS, P99, raw wrk output, background CPU progress and action
evidence. Performance claims are accepted only when background retention meets
the configured floor.

## Nginx Latency Protection

1. Start nginx.
2. Measure baseline with `wrk`.
3. Start `stress-ng --cpu 0`.
4. Run `schedx optimize --target nginx --mode latency_first`.
5. Measure QPS, average latency, p95, and p99 again.

## Redis Latency Protection

```bash
sudo systemctl enable --now redis
sudo schedx benchmark redis \
  --duration 10 --connections 64 --threads 4 \
  --repeats 5 --stress-cpu 4 --warmup 2 \
  --min-background-retention 25 \
  --output results/redis-formal-final
```

This is a fixed-work GET benchmark with three phases: Redis alone, Redis under
CPU interference, and Redis under the existing latency-first AgentLoop. Phase
order rotates across repeats to reduce time-order bias. The summary records
mean, median, standard deviation, a small-sample Student-t 95% confidence
interval, background CPU retention, Agent/scx/cgroup/eBPF evidence, and cleanup.

The verified SP4 run under
`results/redis-formal-final/2026-08-30_01-15-52/` reports a `34.38%` QPS drop
under interference. SchedX recovers `31.03%` QPS versus interference, reduces
P99 by `38.75%`, and retains `61.25%` of background CPU progress. This exceeds
the configured `25%` fairness floor.

## Batch Throughput

```bash
sudo schedx benchmark batch-throughput \
  --duration 20 --threads 4 --repeats 5 \
  --stress-cpu 4 --output results/batch-throughput
```

The benchmark compares sysbench alone, sysbench under CPU interference, and a
SchedX phase. In the SchedX phase, sysbench is already running when the Agent
classifies it and applies the `throughput_first` expert, so the result validates
the live workload pipeline rather than an offline command plan. The phase order
rotates between repeats to reduce time-order bias. A result is valid for claims
only when interference causes a measurable throughput drop, SchedX restores
throughput, and background progress remains above the configured fairness floor.

## Native sched_ext Scheduler Comparison

```bash
sudo schedx benchmark scx-compare \
  --schedulers scx_simple,scx_qmap,scx_flatcg,scx_agent \
  --duration 20 --connections 64 --threads 4 \
  --repeats 5 --stress-cpu 4 --warmup 3 \
  --min-background-retention 25 \
  --output results/scx-compare-formal
```

The default Linux scheduler is an implicit baseline. Every scheduler receives
the same nginx, wrk, and stress-ng configuration. `scx_simple`, `scx_qmap`, and
`scx_flatcg` are measured as lifecycle-only schedulers because they do not
share SchedX's task-policy protocol. `scx_agent` additionally receives explicit
latency and background classes and reports dispatch counters. Results include
five raw wrk samples, mean, median, standard deviation, background CPU
retention, scheduler logs, cleanup evidence, CSV, JSON, and Markdown output.

The verified SP4 run under
`results/scx-compare-formal/2026-08-29_20-14-31/` reports `79.02%` higher mean
RPS and `60.04%` lower mean P99 for `scx_agent` versus default, with `30.48%`
background retention. This exceeds the configured `25%` fairness floor.

## Real eBPF Closed Loop

The Agent loads and attaches four independent BPF programs before resource
actions: scheduler tracing, network policy, resource control, and security
audit. It then writes only structured, allowlisted map entries, samples runtime
counters before verification, and detaches pinned programs during rollback.
Run the Linux-only tests and an Agent loop on the SP4 kernel:

```bash
sudo python3 -m pytest -q
sudo schedx run --max-rounds 1
sudo schedx rollback
test ! -d /sys/fs/bpf/schedx
```

## Final Report

```bash
python3 scripts/generate_competition_report.py \
  --results results \
  --output reports/competition-final.md
```

The report aggregates the latest timestamped demo, ablation, Redis, batch,
canary and native sched_ext summaries. Missing evidence is shown as `n/a`
rather than being inferred or fabricated.

