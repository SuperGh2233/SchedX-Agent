# Persistent sched_ext Daemon

Date: 2026-06-14

## Problem

Linux permits one sched_ext struct_ops scheduler owner at a time. Previously,
each `tool-run` attempted to attach its own `scx_agent`, so concurrent Agent
tools competed for the same kernel interface and later callers fell back to
cgroup-only control.

## Implementation

`schedx scx-daemon run` now owns one long-lived `scx_agent` process and exposes
a JSON-line protocol over:

```text
/run/schedx/scx-daemon.sock
```

Concurrent tool calls register and remove only their own PID policies. Policy
updates are serialized before reaching the existing `scx_agent` userspace
protocol and BPF map.

The daemon supports:

- status and dispatch statistics;
- concurrent task policy registration and removal;
- policy-map inspection;
- graceful shutdown and struct_ops unregister;
- automatic cleanup of policies whose PIDs no longer exist;
- cgroup-level policies inherited by complete tool process trees;
- PSI-driven adaptive cross-class fairness;
- standalone sched_ext and cgroup-only fallback when unavailable.

## systemd Deployment

```bash
sudo install -m 0644 scripts/schedx-scx-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now schedx-scx-daemon
sudo schedx scx-daemon status
```

## openEuler Verification

The reproducible verification used eight concurrent Agent tool calls with
interactive, test, compile, and background intents:

```text
concurrent Agent tools: 8
active policies observed together: 8
successful tools: 8/8
native sched_ext tools: 8/8
daemon-mode tools: 8/8
policies remaining after completion: 0
dead PID policy automatically reaped: true
daemon still running: true
sched_ext state: enabled
nr_rejected: 0
```

Evidence is stored under `results/scx-daemon-concurrency/`.

After the cgroup and fairness upgrade, the eight-Agent regression remained
fully successful. Evidence is stored under
`results/scx-daemon-concurrency-adaptive/`.
