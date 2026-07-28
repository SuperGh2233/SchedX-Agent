#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
auto=false
formal=false
for arg in "$@"; do
    case "$arg" in
        --auto) auto=true ;;
        --formal) formal=true ;;
        -h|--help)
            echo "用法: sudo bash scripts/demo_recording_story.sh [--auto] [--formal]"
            exit 0
            ;;
        *) echo "未知参数: $arg"; exit 2 ;;
    esac
done

if [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi
if [[ -f /etc/schedx/llm.env ]]; then
    set -a
    # shellcheck disable=SC1091
    source /etc/schedx/llm.env
    set +a
fi

pause_scene() {
    $auto || read -r -p $'\n按 Enter 进入下一幕...'
}

banner() {
    [[ -t 1 ]] && clear
    printf '\n\033[1;36m============================================================\033[0m\n'
    printf '\033[1;36m%s\033[0m\n' "$1"
    printf '\033[1;36m============================================================\033[0m\n'
}

cleanup() {
    bash scripts/demo_cleanup.sh >/dev/null 2>&1 || true
}
trap cleanup EXIT

banner "第 1 幕：检查真实运行环境"
bash scripts/demo_cleanup.sh >/dev/null
bash scripts/demo_preflight.sh
pause_scene

banner "第 2 幕：识别在线服务和后台干扰"
stress-ng --cpu 2 --timeout 45s --metrics-brief >.schedx/demo-stress.log 2>&1 &
stress_pid=$!
sleep 2
python3 -m schedx classify --top 50 | python3 -c '
from collections import Counter
import json, sys
c = json.load(sys.stdin)["classification"]

def summary(key):
    rows = c["groups"].get(key, [])
    counts = Counter(x["comm"] for x in rows)
    return ", ".join("{} x{}".format(name, count) for name, count in counts.items()) or "-"

print("场景判断 : {}".format("混合负载" if c["overall"] == "mixed" else c["overall"]))
print("在线服务 : {}".format(summary("latency_sensitive")))
print("后台干扰 : {}".format(summary("background_noise")))
'
pause_scene

banner "第 3 幕：DeepSeek 提出方案，安全规则负责把关"
python3 -m schedx llm-plan --top 50 | python3 -c '
import json, sys
x = json.load(sys.stdin)
d = x["decision"]
p = d.get("parameters", {})
mode_names = {
    "latency_first": "优先保护响应速度",
    "throughput_first": "优先提升处理能力",
    "balanced": "均衡分配资源",
    "isolate_background": "隔离后台干扰",
}
source = "DeepSeek 大模型" if d.get("source") == "deepseek-v4" else d.get("source")
print("方案来源       :", source)
print("优化方向       :", mode_names.get(d.get("mode"), d.get("mode")))
print("保护对象       :", d.get("target"))
print("安全检查       :", d.get("expert_id", "等待系统选择合适方案"))
print("方案可信度     : {:.0%}".format(d.get("confidence", 0)))
print("在线服务优先级 :", p.get("cpu_weight"))
print("后台任务优先级 :", p.get("cpu_weight_bg"))
print("选择原因       : 检测到在线服务和后台干扰同时存在")
'
kill "$stress_pid" 2>/dev/null || true
wait "$stress_pid" 2>/dev/null || true
bash scripts/demo_cleanup.sh >/dev/null
pause_scene

banner "第 4 幕：自动执行、对比效果并检查安全性"
output="results/video-recording"
report="reports/video-recording.md"
args=(
    --output "$output"
    --report "$report"
    --llm-policy
    --compact
)
if $formal; then
    args+=(--formal)
else
    args+=(
        --duration 3
        --repeats 1
        --connections 8
        --threads 2
        --batch-threads 2
        --stress-cpu 2
    )
fi

python3 scripts/run_competition_demo.py "${args[@]}"
pause_scene

banner "第 5 幕：查看性能结果和自动恢复证据"
bash scripts/demo_evidence.sh "$output"
pause_scene

banner "第 6 幕：清理现场并恢复系统默认状态"
bash scripts/demo_cleanup.sh
trap - EXIT
echo
echo "演示完成：发现问题 → 选择方案 → 执行优化 → 检查效果 → 接受或恢复 → 生成报告"
