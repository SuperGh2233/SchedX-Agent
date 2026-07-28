#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

printf '\n=== SchedX-Agent 演示清理 ===\n'
pkill -f '[s]tress-ng' 2>/dev/null || true
python3 -m schedx rollback || true
python3 -m schedx scx-daemon stop >/dev/null 2>&1 || true
sleep 1

stress=false
pgrep -f 'stress-ng|stress-ng-cpu' >/dev/null && stress=true
cgroup=false
[[ -d /sys/fs/cgroup/schedx ]] && cgroup=true
state="$(python3 -m schedx status | python3 -c 'import json,sys; print(json.load(sys.stdin)["sched_ext"]["state"])')"

echo "stress-ng running : $stress"
echo "cgroup remains    : $cgroup"
echo "sched_ext state   : $state"
if [[ $stress == false && $cgroup == false && $state == disabled ]]; then
    echo "CLEAN: 演示环境已恢复"
else
    echo "WARN: 仍有残留，请检查上面三项状态"
    exit 1
fi
