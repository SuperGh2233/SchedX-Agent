#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
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

printf '\n=== SchedX-Agent 录制前预检 ===\n'
[[ ${EUID:-$(id -u)} -eq 0 ]] || { echo "FAIL: 请使用 root 运行"; exit 1; }

for tool in python3 wrk stress-ng sysbench curl; do
    command -v "$tool" >/dev/null || { echo "FAIL: 缺少 $tool"; exit 1; }
done
systemctl is-active --quiet nginx || { echo "FAIL: nginx 未运行"; exit 1; }
curl -fsS http://127.0.0.1/ >/dev/null || { echo "FAIL: nginx 首页不可访问"; exit 1; }

python3 -m schedx status | python3 -c '
import json, sys
d = json.load(sys.stdin)
s = d["sched_ext"]
print("OS/内核       :", d["platform"])
print("cgroup v2     :", d["cgroup_v2"])
print("sched_ext     : available={}, state={}".format(s["available"], s["state"]))
print("Agent mode    :", d["mode"])
print("LLM           : configured={}, model={}".format(d["llm_configured"], d["llm_model"]))
if not d["cgroup_v2"] or not s["available"] or not d["llm_configured"]:
    raise SystemExit(1)
if s["state"] != "disabled":
    print("FAIL: sched_ext 已启用，请先执行 scripts/demo_cleanup.sh")
    raise SystemExit(1)
'

if pgrep -f 'stress-ng|stress-ng-cpu' >/dev/null; then
    echo "FAIL: 检测到残留 stress-ng，请执行 scripts/demo_cleanup.sh"
    exit 1
fi
if [[ -d /sys/fs/cgroup/schedx ]]; then
    echo "FAIL: 检测到残留 /sys/fs/cgroup/schedx，请执行 scripts/demo_cleanup.sh"
    exit 1
fi

echo "READY: 环境、服务、LLM、cgroup v2 与 native sched_ext 均已就绪"
