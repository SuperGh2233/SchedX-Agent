#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== Test tool call: automatic profile, native sched_ext, memory metrics =="
python3 -m schedx tool-run --agent-id demo --intent test -- \
    stress-ng --vm 1 --vm-bytes 64M --timeout 3s --metrics-brief

echo
echo "== Pressure feedback: low memory.high triggers Agent-visible feedback =="
python3 -m schedx tool-run --agent-id demo --intent background \
    --memory-high 32M --memory-max 256M -- \
    stress-ng --vm 1 --vm-bytes 128M --timeout 5s --metrics-brief

echo
echo "Saved tool-call records:"
find .schedx/tool-runs -type f | sort | tail -2
