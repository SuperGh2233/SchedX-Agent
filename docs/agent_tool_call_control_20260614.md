# Agent Tool-Call Resource Control

Date: 2026-06-14

## Competition Value

This feature extends SchedX from traditional process classification to
AI-Agent-aware resource control. Each Agent tool call becomes an ephemeral,
observable, and controllable operating-system resource unit.

The complete loop is:

```text
tool semantics
  -> resource-intent profile
  -> hierarchical ephemeral cgroup
  -> native sched_ext class and weighted-vtime policy
  -> CPU/memory/pids enforcement
  -> pressure metrics
  -> Agent-visible feedback
  -> cleanup
```

## Implemented Profiles

| Intent | Examples | sched_ext class | Main behavior |
|---|---|---:|---|
| interactive | git/status/read | latency | fast response |
| test | pytest/ctest/go test | latency | responsive with larger memory budget |
| compile | make/ninja/gcc | batch | throughput-oriented |
| package | pip/npm/dnf install | batch | bounded CPU and memory |
| background | stress/train/benchmark | background | strongly deprioritized |

## openEuler Verification

A managed test tool call using `stress-ng --vm 1 --vm-bytes 64M` produced:

```text
intent=test
native_scx=true
memory_peak_bytes=178257920
pids_peak=3
returncode=0
sched_ext rejected=0
```

The temporary cgroup was removed after completion and sched_ext returned to
`disabled`.

A pressure-feedback run with `memory.high=32M` emitted:

```text
[schedx-feedback] memory pressure detected; retry with smaller parallelism or a streaming tool
```

This demonstrates bidirectional coordination: the Agent expresses intent before
execution, and the operating system returns actionable resource feedback after
execution.

Agent-to-OS intent can be passed through:

```text
AGENT_RESOURCE_HINT=intent:compile,memory:high,cpu:high
```

Supported hint dimensions currently include `intent`, `memory`, and `cpu`.

Pressure signals close the control loop. When a tool is throttled or reaches
memory pressure, SchedX emits a reusable next-round intent such as:

```text
[schedx-next-hint] intent:test,memory:high,cpu:high
```

The structured JSON record includes `retry_recommended` and
`next_resource_hint`, allowing an Agent to re-plan without parsing prose.

The openEuler fault-injection verification used `memory.high=32M` with a 128M
memory workload. It emitted `intent:test,memory:high`, completed successfully,
removed the ephemeral cgroup, returned sched_ext to `disabled`, and left
`nr_rejected=0`.

## Reproducible Comparison

The formal experiment compares the same Agent tool call under full CPU
contention, first unmanaged and then managed by SchedX:

```bash
sudo python3 scripts/run_tool_call_experiment.py \
  --repeats 5 \
  --output results/tool-call-formal \
  -- python3 -m pytest -q
```

It generates `summary.json`, per-tool-call records, and `report.md`. The report
quantifies tool-call latency, success rate, native sched_ext activation, and
background CPU retention.

The formal 10-repeat openEuler run produced:

```text
unmanaged mean duration: 0.2801 s
managed mean duration:   0.2320 s
tool-call latency reduction: 17.16%
managed native sched_ext runs: 10/10
background CPU retention: 84.78%
sched_ext rejected tasks: 0
```
