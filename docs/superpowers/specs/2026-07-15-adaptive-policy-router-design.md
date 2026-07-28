# SchedX Adaptive Policy Router Design

## Goal

Add an intent-aware mixture-of-policies control layer to SchedX-Agent. The
router stabilizes rule/LLM policy proposals over time, selects one safe expert
mode implemented by the existing `scx_agent`, records outcomes, and requests
rollback when canary metrics show a regression.

## Scope

This phase adds:

- a JSON-backed repository of allowlisted expert policy definitions and
  historical outcomes;
- a time-weighted router with confidence threshold, switch cooldown, and
  consecutive-observation hysteresis;
- four built-in experts: `latency_guard`, `throughput_boost`,
  `background_isolation`, and `balanced`;
- AgentLoop integration for automatic rule and LLM decisions;
- a pure canary verifier for latency, throughput, scheduler rejection, and
  background-starvation checks;
- a read-only `schedx policies` command for demonstration and audit.

This phase does not add:

- runtime loading of arbitrary model-generated BPF code;
- automatic switching between external struct_ops schedulers;
- a trained XGBoost/Random Forest classifier;
- `memcg_bpf_ops`, which requires a different patched kernel;
- changes to the existing cgroup executor or BPF scheduling hot path.

## Architecture

```text
procfs / PSI / cgroup / intent
              |
              v
       rule or LLM proposal
              |
              v
   time-weighted SchedulerRouter
    - confidence threshold
    - cooldown / anti-thrashing
    - allowlisted experts only
              |
              v
    existing PolicySkill + scx_agent
              |
              v
       canary metric verifier
          |             |
       accept        rollback
          |             |
          +------ outcome ------>
                 PolicyRepository
```

The router selects an internal policy mode, not an executable. It therefore
cannot bypass the existing structured action schema, process protection,
dry-run behavior, cgroup rollback, or sched_ext fallback.

## Components

### PolicyRepository

`schedx/policies/repository.py` owns the expert catalog and outcome history.
The default file is `.schedx/policy_repository.json`. A missing file yields the
built-in catalog. Writes use a temporary file followed by `Path.replace()` so
an interrupted update cannot leave partial JSON.

Each expert contains:

- stable `expert_id`;
- existing SchedX mode;
- description and supported workload types;
- safe parameter defaults limited to parameters already accepted by the
  planner/executor;
- aggregate observations, accepts, rejects, and last outcome.

Unknown expert IDs and unsupported modes are rejected.

### SchedulerRouter

`schedx/policies/router.py` converts a classification and the rule/LLM proposal
into expert probabilities. It stores a bounded process-local observation
window and aggregates samples with exponential decay so recent samples have
more weight.

Selection rules:

1. Normalize each sample and aggregate it over the window.
2. Select the highest-scoring allowlisted expert.
3. If confidence is below the threshold, retain the current expert or use
   `balanced` during initialization.
4. If cooldown has not elapsed, retain the current expert.
5. After cooldown, require the candidate to win two consecutive observations.
6. Then switch and record the reason, confidence, scores, and timestamp.

Explicit user modes are never overridden. Automatic rule and LLM proposals
are routed. A route result includes enough metadata for CLI output and reports.

### CanaryVerifier

`schedx/policies/verifier.py` is deterministic and does not execute commands.
It rejects a candidate when any of the following is true:

- sched_ext reports rejected tasks;
- background runtime share falls below the configured floor;
- latency-first P99 regresses beyond the configured percentage;
- throughput-first requests/second regresses beyond the configured percentage;
- a balanced policy causes a severe regression in either metric.

Missing metrics produce an `inconclusive` verdict rather than fabricated
improvements and are counted separately from accepts and rejects. Invalid,
non-finite, or negative metric values are rejected. Existing execution
verification remains unchanged when no canary payload is present.

### AgentLoop Integration

`AgentLoop` owns one router instance so continuous rounds share temporal
history. After the rule engine or LLM produces a proposal, the router selects
an expert and creates a coherent `Decision` using the selected expert mode,
safe parameters, and a target from the corresponding classified process group.

The context stores:

- `agent_decision.source`;
- `agent_decision.expert_id`;
- `policy_route` with scores, confidence, switch state, and reason;
- `canary_verdict` when canary metrics are supplied.

Rejected canaries set `rollback_required`; the loop then runs the existing
RollbackSkill. Accepted policies remain active in continuous mode, while a
successful rollback clears its request before the next round.

## Safety

- The repository contains data, never commands or executable paths.
- Only four allowlisted modes can be selected.
- Explicit CLI modes retain precedence.
- Router code does not write cgroups or BPF maps.
- Dry-run behavior remains owned by the existing context and executor.
- Low confidence falls back to the current expert or `balanced`.
- Canary rejection uses the existing rollback path.

## Testing

Unit tests cover repository persistence and corruption handling, workload to
expert mapping, weighted voting, cooldown, low-confidence fallback, canary
accept/reject/inconclusive results, explicit-mode precedence, AgentLoop route
metadata, and CLI policy listing. The complete Windows test suite must pass;
Linux-only daemon tests remain skipped locally and are run on openEuler before
merge.

## Success Criteria

- `schedx policies` lists the four built-in experts without root privileges.
- Automatic mixed nginx + stress-ng classification selects `latency_guard`.
- A transient contradictory sample does not immediately switch experts.
- Sustained observations after cooldown can switch experts.
- Canary regressions request rollback.
- Existing optimize, cgroup, daemon, benchmark, and dry-run tests remain green.
