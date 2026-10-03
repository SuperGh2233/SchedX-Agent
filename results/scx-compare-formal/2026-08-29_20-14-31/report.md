# SchedX-Agent Nginx sched_ext Scheduler Comparison

The implicit baseline is the default Linux scheduler. Requested scx schedulers are measured under the same nginx and stress-ng workload.

| Scheduler | Capability | Status | Mean RPS | Median RPS | RPS stdev | Mean P99 (ms) | Retention | Valid |
|---|---|---|---:|---:|---:|---:|---:|---|
| default | none | ok | 63804.6080 | 63943.2400 | 946.4829 | 7.4920 | 100.00% | True |
| scx_simple | lifecycle_only | ok | 86021.2220 | 86106.8500 | 936.9359 | 24.0680 | 70.61% | True |
| scx_qmap | lifecycle_only | ok | 5476.4520 | 5482.8700 | 287.4894 | 21.3380 | 179.56% | True |
| scx_flatcg | lifecycle_only | ok | 86154.6280 | 86734.4900 | 1372.9273 | 23.8380 | 70.32% | True |
| scx_agent | task_policy_and_fairness | ok | 114224.9760 | 114588.7100 | 1817.4156 | 2.9940 | 30.48% | True |

## Cleanup

```json
{
  "rollback": {
    "returncode": 0,
    "data": {
      "restored": 0,
      "groups_removed": 0,
      "skipped": 0,
      "entries": [],
      "scx_entries": [],
      "ebpf_cleanup": {}
    },
    "stderr": ""
  },
  "stress_stopped": true,
  "sched_ext_disabled": true,
  "cgroup_clean": true
}
```
