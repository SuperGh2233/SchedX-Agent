#!/usr/bin/env bash
# run_batch_experiment.sh - Batch throughput optimization experiment
#
# Agent autonomously decides strategy based on workload classification.
#
# Usage: sudo bash scripts/run_batch_experiment.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

RESULTS_DIR="${SCHEDX_OUTPUT:-results}"
DURATION="${SCHEDX_DURATION:-30}"
STRESS_CPU="${SCHEDX_STRESS_CPU:-2}"

mkdir -p "$RESULTS_DIR"

cleanup() {
    pkill -f "stress-ng" 2>/dev/null || true
    python3 -m schedx rollback 2>/dev/null || true
}
trap cleanup EXIT

echo "============================================"
echo "  SchedX-Agent Batch Experiment"
echo "============================================"
echo ""

echo "[1/2] Running sysbench with stress-ng (no optimization)..."
stress-ng --cpu "$STRESS_CPU" --timeout "$((DURATION + 15))s" --metrics-brief &
STRESS_PID=$!
sleep 2

python3 -m schedx benchmark batch-cpu \
    --variant default \
    --duration "$DURATION" \
    --output "$RESULTS_DIR"

kill $STRESS_PID 2>/dev/null || true
wait $STRESS_PID 2>/dev/null || true
pkill -f "stress-ng" 2>/dev/null || true
sleep 1
echo ""

echo "[2/2] Agent optimizing for sysbench + running sysbench..."
stress-ng --cpu "$STRESS_CPU" --timeout "$((DURATION + 15))s" --metrics-brief &
STRESS_PID=$!
sleep 2
python3 -m schedx optimize --target stress-ng --mode isolate_background
sleep 1

python3 -m schedx benchmark batch-cpu \
    --variant schedx \
    --duration "$DURATION" \
    --output "$RESULTS_DIR"

kill $STRESS_PID 2>/dev/null || true
wait $STRESS_PID 2>/dev/null || true
echo ""

echo "============================================"
echo "  Results"
echo "============================================"
echo ""

DEFAULT_FILE="$RESULTS_DIR/batch_default.json"
SCHEDX_FILE="$RESULTS_DIR/batch_schedx.json"
if [ -f "$DEFAULT_FILE" ] && [ -f "$SCHEDX_FILE" ]; then
    python3 -c "
import json
with open('$DEFAULT_FILE') as f: d = json.load(f)
with open('$SCHEDX_FILE') as f: s = json.load(f)
dm = d.get('metrics', {})
sm = s.get('metrics', {})
print(f\"  {'Metric':<25} {'No SchedX':>12} {'With SchedX':>12} {'Change':>12}\")
print(f\"  {'-'*61}\")
for key in ['events_per_second', 'elapsed_seconds', 'events']:
    dv = dm.get(key)
    sv = sm.get(key)
    if dv and sv:
        pct = ((sv - dv) / dv) * 100
        label = 'better' if pct > 0 else 'worse'
        print(f'  {key:<25} {dv:>12.2f} {sv:>12.2f} {pct:>+10.2f}% {label}')
"
fi
