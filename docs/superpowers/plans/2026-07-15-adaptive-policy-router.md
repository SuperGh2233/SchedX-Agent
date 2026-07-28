# Adaptive Policy Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe mixture-of-policies router, persistent expert catalog, and canary rollback decision to the existing SchedX Agent loop.

**Architecture:** Rule and LLM decisions remain proposals. A process-local time-weighted router selects one allowlisted expert mode implemented by the existing `scx_agent`; a JSON repository records expert outcomes, and a deterministic verifier rejects regressing canaries through the existing rollback path.

**Tech Stack:** Python 3.11 standard library, dataclasses, JSON, pytest, existing SchedX Skills and AgentContext.

## Global Constraints

- Do not change the native BPF scheduling hot path or cgroup executor.
- Do not execute model-generated commands or scheduler binaries.
- Explicit CLI modes must override automatic routing.
- Preserve existing dry-run and rollback semantics.
- Keep Linux-specific behavior testable through dependency-free pure functions.

---

### Task 1: Expert Policy Repository

**Files:**
- Create: `schedx/policies/repository.py`
- Create: `tests/test_policy_repository.py`

**Interfaces:**
- Produces: `ExpertPolicy`, `PolicyOutcome`, `PolicyRepository.list_experts()`, `PolicyRepository.get()`, and `PolicyRepository.record_outcome()`.

- [ ] **Step 1: Write failing repository tests**

Test that four built-ins load without a file, outcomes survive reload, unknown
experts raise `KeyError`, and malformed JSON falls back to built-ins without
overwriting the malformed file.

- [ ] **Step 2: Verify repository tests fail**

Run: `D:\Anaconda\python.exe -m pytest tests/test_policy_repository.py -q`

Expected: collection fails because `schedx.policies.repository` does not exist.

- [ ] **Step 3: Implement the repository**

Use frozen dataclasses for expert definitions, a versioned JSON shape, and an
atomic temporary-file replacement for outcome writes. Built-in expert modes
must be limited to `latency_first`, `throughput_first`,
`isolate_background`, and `balanced`.

- [ ] **Step 4: Verify repository tests pass**

Run: `D:\Anaconda\python.exe -m pytest tests/test_policy_repository.py -q`

Expected: all repository tests pass.

### Task 2: Time-Weighted Scheduler Router

**Files:**
- Create: `schedx/policies/router.py`
- Create: `tests/test_policy_router.py`

**Interfaces:**
- Consumes: `PolicyRepository.list_experts()` and `PolicyRepository.get()`.
- Produces: `RouteDecision` and `SchedulerRouter.route(classification, proposed_mode, proposed_confidence, now=None)`.

- [ ] **Step 1: Write failing router tests**

Cover mixed latency/background selection, batch selection, low-confidence
balanced fallback, a transient sample held by cooldown, and a sustained switch
after cooldown.

- [ ] **Step 2: Verify router tests fail**

Run: `D:\Anaconda\python.exe -m pytest tests/test_policy_router.py -q`

Expected: collection fails because `schedx.policies.router` does not exist.

- [ ] **Step 3: Implement minimal weighted routing**

Store normalized score maps in a bounded deque. Aggregate with
`decay ** age`, apply confidence threshold, enforce cooldown, and return full
scores and a deterministic reason. Add `target_for_mode()` to select a process
name from the corresponding classification group.

- [ ] **Step 4: Verify router tests pass**

Run: `D:\Anaconda\python.exe -m pytest tests/test_policy_router.py -q`

Expected: all router tests pass.

### Task 3: Canary Safety Verifier

**Files:**
- Create: `schedx/policies/verifier.py`
- Create: `tests/test_policy_verifier.py`

**Interfaces:**
- Produces: `CanaryVerdict` and `CanaryVerifier.evaluate(mode, baseline, candidate, nr_rejected=0, background_share=None)`.

- [ ] **Step 1: Write failing verifier tests**

Cover P99 regression, throughput regression, sched_ext rejection,
background-starvation rejection, successful latency improvement, and missing
metric inconclusive behavior.

- [ ] **Step 2: Verify verifier tests fail**

Run: `D:\Anaconda\python.exe -m pytest tests/test_policy_verifier.py -q`

Expected: collection fails because `schedx.policies.verifier` does not exist.

- [ ] **Step 3: Implement deterministic verification**

Use percentage deltas with division-by-zero protection. Return `accepted`,
`rejected`, or `inconclusive` plus machine-readable reasons and calculated
deltas. Do not read the live system from this module.

- [ ] **Step 4: Verify verifier tests pass**

Run: `D:\Anaconda\python.exe -m pytest tests/test_policy_verifier.py -q`

Expected: all verifier tests pass.

### Task 4: AgentLoop and VerifySkill Integration

**Files:**
- Modify: `schedx/agent/loop.py`
- Modify: `schedx/skills/verify_skill.py`
- Create: `tests/test_policy_router_integration.py`

**Interfaces:**
- Consumes: `SchedulerRouter.route()`, `target_for_mode()`, and `CanaryVerifier.evaluate()`.
- Produces: `context.data["policy_route"]`, routed `agent_decision` metadata, and `context.data["canary_verdict"]`.

- [ ] **Step 1: Write failing integration tests**

Build contexts with synthetic classifications. Assert automatic decisions gain
`expert_id` and route metadata, explicit user modes bypass routing, and a
rejected canary sets `rollback_required`.

- [ ] **Step 2: Verify integration tests fail**

Run: `D:\Anaconda\python.exe -m pytest tests/test_policy_router_integration.py -q`

Expected: assertions fail because route and canary metadata are absent.

- [ ] **Step 3: Wire routing and canary verification**

Create one repository/router per AgentLoop. Route both rule and LLM proposals,
preserve the proposal parameters only when its mode matches the selected
expert, and use the selected expert defaults otherwise. Extend VerifySkill only
when a `canary` payload exists; leave current verification behavior unchanged
otherwise. Route rejected verification to RollbackSkill.

- [ ] **Step 4: Verify integration and regression tests pass**

Run: `D:\Anaconda\python.exe -m pytest tests/test_policy_router_integration.py tests/test_agent_skills.py tests/test_llm_policy.py -q`

Expected: all selected tests pass.

### Task 5: Policy Audit CLI and Documentation

**Files:**
- Modify: `schedx/main.py`
- Modify: `README.md`
- Create: `tests/test_policy_cli.py`

**Interfaces:**
- Produces: `schedx policies` JSON containing repository path, expert definitions, and aggregate outcomes.

- [ ] **Step 1: Write failing CLI test**

Parse `policies` and call its handler against a temporary state directory.
Assert four experts are returned without requiring root.

- [ ] **Step 2: Verify CLI test fails**

Run: `D:\Anaconda\python.exe -m pytest tests/test_policy_cli.py -q`

Expected: parser rejects the missing `policies` command.

- [ ] **Step 3: Add the read-only command and README section**

The command may read repository JSON but must not touch cgroups, sched_ext, or
the network. Document the expert modes, anti-thrashing behavior, canary safety,
and openEuler verification commands.

- [ ] **Step 4: Run full verification**

Run:

```powershell
D:\Anaconda\python.exe -m compileall -q schedx
D:\Anaconda\python.exe -m pytest -q
D:\Anaconda\python.exe -m schedx.main policies
D:\Anaconda\python.exe -m schedx.main optimize --target nginx --mode latency_first --dry-run
```

Expected: compile succeeds, all Windows tests pass with Linux-only skips, the
policy catalog contains four experts, and explicit dry-run optimization remains
unchanged.

