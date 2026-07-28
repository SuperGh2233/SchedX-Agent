#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
result_root="${1:-results/video-recording}"
latest="$result_root/latest.json"
[[ -f $latest ]] || { echo "未找到 $latest"; exit 1; }

python3 - "$latest" <<'PY'
import json
import sys
from pathlib import Path

latest = Path(sys.argv[1])
run_dir = Path(json.loads(latest.read_text(encoding="utf-8"))["run_dir"])
data = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

def metric(value, digits=2):
    return "N/A" if value is None else f"{value:.{digits}f}"

def percentage(value):
    return "N/A" if value is None else f"{value:.2f}%"

def change(before, after, lower_is_better=False):
    if before in (None, 0) or after is None:
        return "N/A"
    delta = (after - before) / before * 100.0
    if abs(delta) < 0.005:
        return "0.00% (unchanged)"
    improved = delta < 0 if lower_is_better else delta > 0
    arrow = "↓" if delta < 0 else "↑"
    result = "improved" if improved else "regressed"
    return f"{arrow}{abs(delta):.2f}% ({result})"

def measured_change(value, lower_is_better=False):
    if value is None:
        return "N/A"
    if abs(value) < 0.005:
        return "0.00% (unchanged)"
    improved = value < 0 if lower_is_better else value > 0
    arrow = "↓" if value < 0 else "↑"
    result = "improved" if improved else "regressed"
    return f"{arrow}{abs(value):.2f}% ({result})"

print("\n=== SchedX-Agent 完整证据链 ===")
print(f"运行目录 : {run_dir}")
print(f"运行状态 : {data.get('status')}")
print(f"实验参数 : duration={data.get('duration')}s, repeats={data.get('repeats')}")

nginx = data["nginx_ablation"]["summary"]["phases"]
print("\n[四组消融]                 RPS        P99(ms)   后台保留率")
for key, label in (
    ("default", "Default"),
    ("cgroup_only", "cgroup-only"),
    ("scx_only", "scx-only"),
    ("agent_combined", "Agent-combined"),
):
    row = nginx[key]
    print(
        f"{label:<16} {metric(row.get('mean_requests_per_sec')):>10}"
        f" {metric(row.get('mean_p99_ms'), 3):>12}"
        f" {percentage(row.get('background_retention_percent')):>11}"
    )

default = nginx["default"]
agent = nginx["agent_combined"]
print(
    "Agent result       : RPS {}, P99 {}, background retained {}".format(
        change(
            default.get("mean_requests_per_sec"),
            agent.get("mean_requests_per_sec"),
        ),
        change(
            default.get("mean_p99_ms"),
            agent.get("mean_p99_ms"),
            lower_is_better=True,
        ),
        percentage(agent.get("background_retention_percent")),
    )
)

batch = data["batch_throughput"]["summary"]["phases"]
print("\n[第二 workload：sysbench]")
for key, label in (("baseline", "baseline"), ("interference", "interference"), ("schedx", "SchedX")):
    row = batch[key]
    print(
        f"{label:<14} events/s={metric(row.get('mean_events_per_second'))}, "
        f"P95={metric(row.get('mean_latency_p95_ms'), 3)} ms"
    )

interference = batch["interference"]
schedx = batch["schedx"]
print(
    "SchedX vs interference: throughput {}, P95 {}".format(
        change(
            interference.get("mean_events_per_second"),
            schedx.get("mean_events_per_second"),
        ),
        change(
            interference.get("mean_latency_p95_ms"),
            schedx.get("mean_latency_p95_ms"),
            lower_is_better=True,
        ),
    )
)

trace = data["agent_trace"]
for key, label in (("accepted", "Canary 常规门槛"), ("rejected", "Canary 严格安全门槛")):
    item = trace[key]
    decision = item.get("decision", {})
    verdict = item.get("canary_verdict", {})
    deltas = verdict.get("deltas", {})
    print(f"\n[{label}]")
    print(
        f"Policy  : source={decision.get('source')}, expert={decision.get('expert_id')}, "
        f"mode={decision.get('mode')}, target={decision.get('target')}"
    )
    print(
        "Verdict : {}".format(
            "ROLLED_BACK"
            if item.get("final_status") == "rolled_back"
            else str(verdict.get("status", item.get("final_status", ""))).upper()
        )
    )
    print(
        "Metrics : P99 {}, RPS {}, background retained {}".format(
            measured_change(deltas.get("p99_percent"), lower_is_better=True),
            measured_change(deltas.get("requests_per_sec_percent")),
            percentage(deltas.get("background_retention_percent")),
        )
    )
    if verdict.get("reasons"):
        print(f"Reasons : {', '.join(verdict['reasons'])}")
    if item.get("rollback"):
        rollback = item["rollback"]
        print(
            "Rollback: restored={}, groups_removed={}, scx_entries_removed={}".format(
                rollback.get("restored", 0),
                rollback.get("groups_removed", 0),
                rollback.get("scx_entries_removed", 0),
            )
        )

cleanup = data["cleanup"]
print("\n[最终清理]")
print(
    f"stress_ng_running={cleanup['stress_ng_running']}, "
    f"cgroup_base_exists={cleanup['cgroup_base_exists']}, "
    f"sched_ext_state={cleanup['sched_ext_state']}"
)
print(f"报告文件 : {data.get('report', {}).get('path', '')}")
PY
