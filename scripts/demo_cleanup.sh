#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

printf '\n=== SchedX-Agent 演示清理 ===\n'
pkill -f '[s]tress-ng' 2>/dev/null || true
rollback_output="$(python3 -m schedx rollback 2>/dev/null || true)"
if [[ -n $rollback_output ]]; then
    printf '%s' "$rollback_output" | python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    print("资源设置恢复完成")
else:
    restored = data.get("restored", len(data.get("rolled_back", [])))
    removed = data.get("groups_removed", 0)
    print(f"恢复设置数量     : {restored}")
    print(f"删除资源组数量   : {removed}")
'
fi
python3 -m schedx scx-daemon stop >/dev/null 2>&1 || true
sleep 1

stress=false
pgrep -f 'stress-ng|stress-ng-cpu' >/dev/null && stress=true
cgroup=false
[[ -d /sys/fs/cgroup/schedx ]] && cgroup=true
state="$(python3 -m schedx status | python3 -c 'import json,sys; print(json.load(sys.stdin)["sched_ext"]["state"])')"
state_text="$state"
[[ $state == disabled ]] && state_text="已关闭"
[[ $state == enabled ]] && state_text="运行中"

echo "后台干扰仍在运行 : $([[ $stress == true ]] && echo 是 || echo 否)"
echo "资源控制仍有残留 : $([[ $cgroup == true ]] && echo 是 || echo 否)"
echo "自定义调度状态   : $state_text"
if [[ $stress == false && $cgroup == false && $state == disabled ]]; then
    echo "CLEAN: 演示环境已恢复"
else
    echo "WARN: 仍有残留，请检查上面三项状态"
    exit 1
fi
