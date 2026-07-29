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

1. Start redis-server.
2. Measure with `redis-benchmark`.
3. Start CPU interference.
4. Run `schedx optimize --target redis --mode latency_first --apply`.
5. Compare throughput and tail latency.

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

## Final Report

```bash
python3 scripts/generate_competition_report.py \
  --results results \
  --output reports/competition-final.md
```

The report aggregates the latest timestamped demo, ablation, batch, canary and
native sched_ext summaries. Missing evidence is shown as `n/a` rather than
being inferred or fabricated.

