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

print("\n=== SchedX-Agent 完整证据链 ===")
print(f"运行目录 : {run_dir}")
print(f"运行状态 : {data.get('status')}")
print(f"实验参数 : duration={data.get('duration')}s, repeats={data.get('repeats')}")

nginx = data["nginx_ablation"]["summary"]["phases"]
print("\n[四组消融]                 RPS        P99(ms)   后台保留率")
for key, label in (
    ("default", "默认调度"),
    ("cgroup_only", "cgroup-only"),
    ("scx_only", "scx-only"),
    ("agent_combined", "Agent 联合"),
):
    row = nginx[key]
    print(
        f"{label:<16} {metric(row.get('mean_requests_per_sec')):>10}"
        f" {metric(row.get('mean_p99_ms'), 3):>12}"
        f" {percentage(row.get('background_retention_percent')):>11}"
    )

batch = data["batch_throughput"]["summary"]["phases"]
print("\n[第二 workload：sysbench]")
for key, label in (("baseline", "baseline"), ("interference", "interference"), ("schedx", "SchedX")):
    row = batch[key]
    print(
        f"{label:<14} events/s={metric(row.get('mean_events_per_second'))}, "
        f"P95={metric(row.get('mean_latency_p95_ms'), 3)} ms"
    )

trace = data["agent_trace"]
for key, label in (("accepted", "Canary 常规门槛"), ("rejected", "Canary 严格门槛")):
    item = trace[key]
    decision = item.get("decision", {})
    verdict = item.get("canary_verdict", {})
    print(f"\n[{label}]")
    print(
        f"DeepSeek source={decision.get('source')}, expert={decision.get('expert_id')}, "
        f"mode={decision.get('mode')}, target={decision.get('target')}"
    )
    print(f"final_status={item.get('final_status')}, reasons={verdict.get('reasons', [])}")
    print(f"deltas={verdict.get('deltas', {})}")
    if item.get("rollback"):
        print(f"rollback={item['rollback']}")

cleanup = data["cleanup"]
print("\n[最终清理]")
print(
    f"stress_ng_running={cleanup['stress_ng_running']}, "
    f"cgroup_base_exists={cleanup['cgroup_base_exists']}, "
    f"sched_ext_state={cleanup['sched_ext_state']}"
)
print(f"报告文件 : {data.get('report', {}).get('path', '')}")
PY
