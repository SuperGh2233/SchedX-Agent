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

banner "第 1 幕：环境预检——证明执行面是真实的"
bash scripts/demo_cleanup.sh >/dev/null
bash scripts/demo_preflight.sh
pause_scene

banner "第 2 幕：制造混合负载——nginx 在线服务 + stress-ng 干扰"
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

print("Overall workload : {}".format(c["overall"].upper()))
print("Online services  : {}".format(summary("latency_sensitive")))
print("CPU interference : {}".format(summary("background_noise")))
'
pause_scene

banner "第 3 幕：DeepSeek 提案——受约束策略进入专家路由"
python3 -m schedx llm-plan --top 50 | python3 -c '
import json, sys
x = json.load(sys.stdin)
d = x["decision"]
print("source     =", d.get("source"))
print("mode       =", d.get("mode"))
print("target     =", d.get("target"))
print("expert     =", d.get("expert_id", "pending safety routing"))
print("confidence =", d.get("confidence"))
print("parameters =", d.get("parameters"))
print("reason     =", d.get("reason"))
'
kill "$stress_pid" 2>/dev/null || true
wait "$stress_pid" 2>/dev/null || true
bash scripts/demo_cleanup.sh >/dev/null
pause_scene

banner "第 4 幕：真实闭环——消融、第二 workload、Canary 接受与回滚"
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

banner "第 5 幕：证据回放——性能收益、Agent trace 与原子回滚"
bash scripts/demo_evidence.sh "$output"
pause_scene

banner "第 6 幕：最终清理——恢复默认调度状态且无资源残留"
bash scripts/demo_cleanup.sh
trap - EXIT
echo
echo "演示闭环完成：感知 → 决策 → 执行 → 验证 → 接受或回滚 → 报告与清理"
