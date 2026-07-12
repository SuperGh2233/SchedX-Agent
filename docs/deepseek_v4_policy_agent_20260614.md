# DeepSeek V4 Policy Agent

Date: 2026-06-14

## Architecture

DeepSeek V4-Pro now participates directly in the scheduling decision phase:

```text
Probe -> Classify -> Rule Baseline -> DeepSeek V4 Proposal
      -> Local Safety Validation -> Policy -> scx/cgroup Act
      -> Verify -> Rollback or Continue
```

This is intentionally not an unrestricted shell Agent. The LLM can propose
only a structured scheduling policy. Local code remains responsible for
validation, execution, verification, and rollback.

## Safety Boundary

Allowed modes:

- `latency_first`
- `throughput_first`
- `balanced`
- `isolate_background`

Allowed parameters are restricted to bounded CPU weights, bounded nice values,
and an allowlist of CPU quotas. Unknown parameters are discarded. Unsafe values
are rejected. A target must exist in the current workload classification;
otherwise SchedX uses the safe rule-engine target.

API errors, invalid JSON, unavailable models, or rejected proposals trigger a
deterministic rule-engine fallback.

Free-form `llm-analyze` output remains advisory only and is never executable.
Only the validated structured output from `llm-plan` can enter the policy
pipeline.

## Secret Management

The API key is not stored in source code, documentation, tests, or result
files. The default external configuration path is:

```text
/etc/schedx/llm.env
```

It is installed with mode `0600`. The model is configured as
`deepseek-v4-pro`.

## Real openEuler Verification

`schedx llm-plan` made a real DeepSeek V4-Pro API call and returned a validated
policy:

```text
mode: latency_first
target: redis-server
source: deepseek-v4
confidence: 0.9
```

`schedx optimize --llm-policy --dry-run` then passed that model-generated
decision through the complete Agent pipeline and previewed six native scx
policies. The daemon remained active, sched_ext remained enabled, and
`nr_rejected=0`.

Evidence:

- `results/llm-agent/llm-plan.json`
- `results/llm-agent/llm-optimize-dry-run.json`

## Commands

```bash
schedx status
schedx llm-plan
schedx optimize --llm-policy --dry-run
schedx run --llm-policy --max-rounds 3
```
