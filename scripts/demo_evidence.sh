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
        return "0.00%（基本不变）"
    improved = delta < 0 if lower_is_better else delta > 0
    arrow = "↓" if delta < 0 else "↑"
    result = "改善" if improved else "回退"
    return f"{arrow}{abs(delta):.2f}%（{result}）"

def measured_change(value, lower_is_better=False):
    if value is None:
        return "N/A"
    if abs(value) < 0.005:
        return "0.00%（基本不变）"
    improved = value < 0 if lower_is_better else value > 0
    arrow = "↓" if value < 0 else "↑"
    result = "改善" if improved else "回退"
    return f"{arrow}{abs(value):.2f}%（{result}）"

def source_name(value):
    return {
        "deepseek-v4": "DeepSeek 大模型",
        "explicit_cli": "固定安全规则",
        "rule_fallback": "本地规则",
    }.get(value, value or "未知")

def mode_name(value):
    return {
        "latency_first": "优先保护响应速度",
        "throughput_first": "优先提升处理能力",
        "balanced": "均衡分配资源",
        "isolate_background": "隔离后台干扰",
    }.get(value, value or "未知")

def expert_name(value):
    return {
        "latency_guard": "响应速度保护方案",
        "background_isolation": "后台隔离方案",
        "balanced": "均衡方案",
        "throughput_boost": "处理能力提升方案",
    }.get(value, value or "等待选择")

print("\n=== SchedX-Agent 演示结果 ===")
print(f"运行目录 : {run_dir}")
print(f"运行状态 : {data.get('status')}")
print(f"测试设置 : 每组 {data.get('duration')} 秒，重复 {data.get('repeats')} 次")

nginx = data["nginx_ablation"]["summary"]["phases"]
print("\n[四种方案对比]")
for key, label in (
    ("default", "默认调度"),
    ("cgroup_only", "仅限制后台资源"),
    ("scx_only", "仅使用自定义调度"),
    ("agent_combined", "Agent 联合优化"),
):
    row = nginx[key]
    print(
        f"{label}: 每秒请求 {metric(row.get('mean_requests_per_sec'))}，"
        f"最慢 1% 延迟 {metric(row.get('mean_p99_ms'), 3)} 毫秒，"
        f"后台任务进度 {percentage(row.get('background_retention_percent'))}"
    )

default = nginx["default"]
agent = nginx["agent_combined"]
print(
    "Agent 综合效果：每秒请求数 {}，最慢 1% 延迟 {}，后台任务进度 {}".format(
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
print("\n[第二类任务：批处理计算]")
for key, label in (("baseline", "无干扰基准"), ("interference", "加入后台干扰"), ("schedx", "Agent 优化后")):
    row = batch[key]
    print(
        f"{label}: 每秒完成 {metric(row.get('mean_events_per_second'))} 次，"
        f"较慢请求延迟 {metric(row.get('mean_latency_p95_ms'), 3)} 毫秒"
    )

interference = batch["interference"]
schedx = batch["schedx"]
print(
    "Agent 相比干扰场景：处理能力 {}，较慢请求延迟 {}".format(
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
for key, label in (("accepted", "常规小范围试运行"), ("rejected", "严格安全检查")):
    item = trace[key]
    decision = item.get("decision", {})
    verdict = item.get("canary_verdict", {})
    deltas = verdict.get("deltas", {})
    print(f"\n[{label}]")
    print(
        f"方案：来源={source_name(decision.get('source'))}，"
        f"选择={expert_name(decision.get('expert_id'))}，"
        f"方向={mode_name(decision.get('mode'))}，保护对象={decision.get('target')}"
    )
    print(
        "结果：{}".format(
            "已自动恢复（ROLLED_BACK）"
            if item.get("final_status") == "rolled_back"
            else "已接受（ACCEPTED）"
            if verdict.get("status") == "accepted"
            else str(verdict.get("status", item.get("final_status", ""))).upper()
        )
    )
    print(
        "变化：最慢 1% 延迟 {}，每秒请求数 {}，后台任务进度 {}".format(
            measured_change(deltas.get("p99_percent"), lower_is_better=True),
            measured_change(deltas.get("requests_per_sec_percent")),
            percentage(deltas.get("background_retention_percent")),
        )
    )
    if verdict.get("reasons"):
        reason_names = {
            "insufficient_p99_improvement": "未达到严格的延迟改善目标",
            "throughput_regression": "每秒请求数下降超过安全范围",
            "background_progress_regression": "后台任务进度下降超过安全范围",
            "background_starvation": "后台任务获得的处理器时间过少",
            "missing_background_progress": "没有采集到后台任务进度",
        }
        print(
            "原因："
            + "，".join(reason_names.get(reason, reason) for reason in verdict["reasons"])
        )
    if item.get("rollback"):
        rollback = item["rollback"]
        print(
            "自动恢复：恢复 {} 项设置，删除 {} 个资源组和 {} 个调度策略".format(
                rollback.get("restored", 0),
                rollback.get("groups_removed", 0),
                rollback.get("scx_entries_removed", 0),
            )
        )

cleanup = data["cleanup"]
print("\n[环境恢复]")
print(
    f"后台干扰已停止={not cleanup['stress_ng_running']}，"
    f"资源控制已清理={not cleanup['cgroup_base_exists']}，"
    "自定义调度={}".format(
        "已关闭（disabled）"
        if cleanup["sched_ext_state"] == "disabled"
        else cleanup["sched_ext_state"]
    )
)
print(f"报告文件 : {data.get('report', {}).get('path', '')}")
PY
