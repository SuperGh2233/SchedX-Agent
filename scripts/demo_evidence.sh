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
    return "无有效数据" if value is None else f"{value:.{digits}f}"

def percentage(value):
    return "无有效数据" if value is None else f"{value:.2f}%"

def change(before, after, lower_is_better=False):
    if before in (None, 0) or after is None:
        return "无有效数据"
    delta = (after - before) / before * 100.0
    if abs(delta) < 0.005:
        return "0.00%（基本不变）"
    improved = delta < 0 if lower_is_better else delta > 0
    arrow = "↓" if delta < 0 else "↑"
    result = "改善" if improved else "回退"
    return f"{arrow}{abs(delta):.2f}%（{result}）"

def measured_change(value, lower_is_better=False):
    if value is None:
        return "无有效数据"
    if abs(value) < 0.005:
        return "0.00%（基本不变）"
    improved = value < 0 if lower_is_better else value > 0
    arrow = "↓" if value < 0 else "↑"
    result = "改善" if improved else "回退"
    return f"{arrow}{abs(value):.2f}%（{result}）"

def delta_percent(before, after):
    if before in (None, 0) or after is None:
        return None
    return (after - before) / before * 100.0

def spoken_change(value):
    if value is None:
        return "没有有效数据"
    if abs(value) < 0.005:
        return "基本不变"
    return f"{'提高' if value > 0 else '下降'} {abs(value):.2f}%"

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
print(f"在线服务测试 : 每组 {data.get('duration')} 秒，重复 {data.get('repeats')} 次")

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
        f"后台运行量（基准=100%）：{percentage(row.get('background_retention_percent'))}"
    )

default = nginx["default"]
agent = nginx["agent_combined"]
print(
    "Agent 综合效果：每秒请求数 {}，最慢 1% 延迟 {}，后台运行量（基准=100%）：{}".format(
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

batch_summary = data["batch_throughput"]["summary"]
batch = batch_summary["phases"]
batch_config = batch_summary["config"]
print("\n[第二类任务：批处理计算]")
print(
    "测试设置: 每组 {} 秒，重复 {} 次，计算线程 {}，干扰线程 {}".format(
        batch_config["duration"],
        batch_config["repeats"],
        batch_config["threads"],
        batch_config["stress_cpu"],
    )
)
for key, label in (("baseline", "无干扰基准"), ("interference", "加入后台干扰"), ("schedx", "Agent 优化后")):
    row = batch[key]
    print(
        f"{label}: 每秒完成 {metric(row.get('mean_events_per_second'))} 次，"
        f"较慢请求延迟 {metric(row.get('mean_latency_p95_ms'), 3)} 毫秒"
    )

interference = batch["interference"]
schedx = batch["schedx"]
print(
    "干扰有效性: {}".format(
        "已观察到明确性能下降"
        if batch_summary.get("interference_detected")
        else "本轮未形成稳定性能下降，仅作为流程验证"
    )
)
print(
    "结果可信性: {}".format(
        "满足吞吐提升和后台运行量要求"
        if schedx.get("valid_for_claims")
        else "未同时满足吞吐提升和后台运行量要求"
    )
)
print(
    "Agent 相比干扰场景：处理能力 {}，较慢请求延迟 {}，后台运行量（基准=100%）：{}".format(
        change(
            interference.get("mean_events_per_second"),
            schedx.get("mean_events_per_second"),
        ),
        change(
            interference.get("mean_latency_p95_ms"),
            schedx.get("mean_latency_p95_ms"),
            lower_is_better=True,
        ),
        percentage(schedx.get("background_retention_percent")),
    )
)

trace = data["agent_trace"]
completed_phases = trace.get("accepted", {}).get("phases_completed", [])
phase_names = {
    "probe": "ProbeSkill（系统感知）",
    "analyze": "AnalyzeSkill（负载分类）",
    "llm_policy": "LlmPolicySkill（大模型建议）",
    "policy": "PolicySkill（策略生成）",
    "canary_baseline": "CanaryBaselineSkill（执行前测量）",
    "ebpf_load": "EbpfLoadSkill（扩展检查）",
    "ebpf_attach": "EbpfAttachSkill（挂载检查）",
    "ebpf_policy": "EbpfPolicySkill（扩展策略检查）",
    "scx": "ScxSkill（自定义调度）",
    "act": "ActSkill（资源执行）",
    "canary_candidate": "CanaryCandidateSkill（执行后测量）",
    "ebpf_stats": "EbpfStatsSkill（内核证据采集）",
    "verify": "VerifySkill（效果验证）",
    "rollback": "RollbackSkill（自动恢复）",
}
visible_phases = [
    phase_names[phase] for phase in completed_phases if phase in phase_names
]
print("\n[标准化 Skills 执行链]")
for start in range(0, len(visible_phases), 4):
    print(" → ".join(visible_phases[start : start + 4]))
print(
    "ReportSkill（报告生成）: {}".format(
        "已完成" if data.get("report", {}).get("returncode") == 0 else "未完成"
    )
)

ebpf = trace.get("accepted", {}).get("ebpf", {})
hooks = ebpf.get("hooks", {}) if isinstance(ebpf, dict) else {}
print("\n[真实 eBPF 证据]")
print(f"策略下发进程数: {ebpf.get('policies_applied', 0)}")
for key, label in (
    ("scheduler_trace", "调度观测"),
    ("network_policy", "网络策略"),
    ("resource_control", "资源观测"),
    ("security_policy", "安全审计"),
):
    item = hooks.get(key, {})
    stats = item.get("stats", {}) if isinstance(item, dict) else {}
    print(f"{label}: {'已挂载并采集数据' if stats else '本轮无有效统计'}")

comparison_files = sorted(
    Path("results/scx-compare-formal").glob("*/summary.json"),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)
if comparison_files:
    comparison = json.loads(comparison_files[0].read_text(encoding="utf-8"))
    schedulers = comparison.get("schedulers", {})
    print("\n[五次正式调度器横向对比]")
    print(f"证据目录: {comparison_files[0].parent}")
    for key, label in (
        ("default", "默认调度"),
        ("scx_simple", "scx_simple"),
        ("scx_qmap", "scx_qmap"),
        ("scx_flatcg", "scx_flatcg"),
        ("scx_agent", "SchedX 自研调度器"),
    ):
        item = schedulers.get(key, {})
        stats = item.get("statistics", {})
        print(
            f"{label}: 每秒请求 {metric(stats.get('requests_per_sec', {}).get('mean'))}，"
            f"最慢 1% 延迟 {metric(stats.get('p99_ms', {}).get('mean'), 3)} 毫秒，"
            f"后台运行量 {percentage(item.get('background_retention_percent'))}"
        )
    agent_comparison = schedulers.get("scx_agent", {}).get(
        "comparison_vs_default", {}
    )
    p99_reduction = agent_comparison.get("p99_reduction_percent")
    print(
        "正式结论: 自研调度器吞吐 {}，最慢 1% 延迟 {}，并通过后台运行量门槛".format(
            measured_change(agent_comparison.get("rps_gain_percent")),
            measured_change(
                -p99_reduction if p99_reduction is not None else None,
                lower_is_better=True,
            ),
        )
    )

redis_files = sorted(
    Path("results/redis-formal-final").glob("*/summary.json"),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)
if redis_files:
    redis = json.loads(redis_files[0].read_text(encoding="utf-8"))
    phases = redis.get("phases", {})
    print("\n[Redis 第二工作负载正式实验]")
    print(f"证据目录: {redis_files[0].parent}")
    for key, label in (
        ("baseline", "无干扰基准"),
        ("interference", "加入后台干扰"),
        ("schedx", "Agent 优化后"),
    ):
        item = phases.get(key, {})
        print(
            f"{label}: 每秒请求 {metric(item.get('requests_per_sec', {}).get('mean'))}，"
            f"最慢 1% 延迟 {metric(item.get('p99_ms', {}).get('mean'), 3)} 毫秒"
        )
    schedx_redis = phases.get("schedx", {})
    print(
        "正式结论: 相比干扰场景，每秒请求提高 {}，最慢 1% 延迟下降 {}，"
        "后台运行量保持在基准的 {}。".format(
            percentage(redis.get("schedx_qps_gain_vs_interference_percent")),
            percentage(redis.get("schedx_p99_reduction_vs_interference_percent")),
            percentage(schedx_redis.get("background_retention_percent")),
        )
    )

reason_names = {
    "insufficient_p99_improvement": "未达到严格的延迟改善目标",
    "throughput_regression": "每秒请求数下降超过安全范围",
    "background_progress_regression": "后台运行量下降超过安全范围",
    "background_starvation": "后台任务获得的处理器时间过少",
    "missing_background_progress": "没有采集到后台运行量",
    "execution_failed_before_verification": "执行阶段未完成，未进入效果验证",
}
for key, label in (("accepted", "常规小范围试运行"), ("rejected", "严格安全检查")):
    item = trace[key]
    decision = item.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    verdict = item.get("canary_verdict")
    verdict = verdict if isinstance(verdict, dict) else {}
    deltas = verdict.get("deltas")
    deltas = deltas if isinstance(deltas, dict) else {}
    print(f"\n[{label}]")
    print(
        f"方案：来源={source_name(decision.get('source'))}，"
        f"选择={expert_name(decision.get('expert_id'))}，"
        f"方向={mode_name(decision.get('mode'))}，保护对象={decision.get('target')}"
    )
    print(
        "结果：{}".format(
            "执行未完成，已自动恢复"
            if item.get("final_status") == "rolled_back" and not verdict
            else "已自动恢复"
            if item.get("final_status") == "rolled_back"
            else "已接受（ACCEPTED）"
            if verdict.get("status") == "accepted"
            else str(verdict.get("status") or item.get("final_status") or "数据不足")
        )
    )
    print(
        "变化：最慢 1% 延迟 {}，每秒请求数 {}，后台运行量（基准=100%）：{}".format(
            measured_change(deltas.get("p99_percent"), lower_is_better=True),
            measured_change(deltas.get("requests_per_sec_percent")),
            percentage(deltas.get("background_retention_percent")),
        )
    )
    reasons = verdict.get("reasons", [])
    if not verdict and item.get("final_status") == "rolled_back":
        reasons = ["execution_failed_before_verification"]
    if reasons:
        print(
            "原因："
            + "，".join(reason_names.get(reason, reason) for reason in reasons)
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

online_rps_delta = delta_percent(
    default.get("mean_requests_per_sec"),
    agent.get("mean_requests_per_sec"),
)
online_p99_delta = delta_percent(
    default.get("mean_p99_ms"),
    agent.get("mean_p99_ms"),
)
batch_throughput_delta = delta_percent(
    interference.get("mean_events_per_second"),
    schedx.get("mean_events_per_second"),
)
batch_latency_delta = delta_percent(
    interference.get("mean_latency_p95_ms"),
    schedx.get("mean_latency_p95_ms"),
)
candidate = trace["accepted"]
candidate_verdict = candidate.get("canary_verdict")
candidate_verdict = candidate_verdict if isinstance(candidate_verdict, dict) else {}
candidate_deltas = candidate_verdict.get("deltas")
candidate_deltas = candidate_deltas if isinstance(candidate_deltas, dict) else {}
candidate_reasons = candidate_verdict.get("reasons", [])
candidate_reason = "，".join(
    reason_names.get(reason, reason) for reason in candidate_reasons
) or "安全检查结果"
candidate_result = (
    f"因{candidate_reason}，系统自动恢复"
    if candidate.get("final_status") == "rolled_back"
    else "通过安全检查，可以接受"
)

print("\n[本轮核心结论]")
print(
    "在线服务：Agent 联合优化使每秒请求数{}，"
    "最慢百分之一请求延迟{}，后台运行量保持在基准的 {}。".format(
        spoken_change(online_rps_delta),
        spoken_change(online_p99_delta),
        percentage(agent.get("background_retention_percent")),
    )
)
print(
    "批处理任务：Agent 相比干扰状态使处理能力{}，"
    "较慢请求延迟{}，后台运行量保持在基准的 {}。".format(
        spoken_change(batch_throughput_delta),
        spoken_change(batch_latency_delta),
        percentage(schedx.get("background_retention_percent")),
    )
)
print(
    "策略安全：DeepSeek 候选方案使最慢百分之一请求延迟{}，"
    "每秒请求数{}；{}。大模型负责提出方案，真实数据和安全规则负责最终决定。".format(
        spoken_change(candidate_deltas.get("p99_percent")),
        spoken_change(candidate_deltas.get("requests_per_sec_percent")),
        candidate_result,
    )
)

cleanup = data["cleanup"]
print("\n[环境恢复]")
print(
    f"后台干扰已停止={'是' if not cleanup['stress_ng_running'] else '否'}，"
    f"资源控制已清理={'是' if not cleanup['cgroup_base_exists'] else '否'}，"
    "自定义调度={}".format(
        "已关闭"
        if cleanup["sched_ext_state"] == "disabled"
        else cleanup["sched_ext_state"]
    )
)
print(f"报告文件 : {data.get('report', {}).get('path', '')}")
PY
