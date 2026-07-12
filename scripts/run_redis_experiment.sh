#!/usr/bin/env bash
# run_redis_experiment.sh - Redis latency protection experiment
#
# Agent autonomously decides strategy based on workload classification.
#
# Usage: sudo bash scripts/run_redis_experiment.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

RESULTS_DIR="${SCHEDX_OUTPUT:-results}"
DURATION="${SCHEDX_DURATION:-10}"
STRESS_CPU="${SCHEDX_STRESS_CPU:-2}"

mkdir -p "$RESULTS_DIR"

cleanup() {
    pkill -f "stress-ng" 2>/dev/null || true
    python3 -m schedx rollback 2>/dev/null || true
}
trap cleanup EXIT

echo "============================================"
echo "  SchedX-Agent Redis Experiment"
echo "============================================"
echo ""

echo "[1/2] Running redis-benchmark with stress-ng (no optimization)..."
stress-ng --cpu "$STRESS_CPU" --timeout "$((DURATION + 10))s" --metrics-brief &
STRESS_PID=$!
sleep 2

python3 -m schedx benchmark redis-latency \
    --variant default \
    --duration "$DURATION" \
    --output "$RESULTS_DIR"

kill $STRESS_PID 2>/dev/null || true
wait $STRESS_PID 2>/dev/null || true
pkill -f "stress-ng" 2>/dev/null || true
sleep 1
echo ""

echo "[2/2] Agent optimizing for redis + running redis-benchmark..."
stress-ng --cpu "$STRESS_CPU" --timeout "$((DURATION + 10))s" --metrics-brief &
STRESS_PID=$!
sleep 2
python3 -m schedx optimize --target redis
sleep 1

python3 -m schedx benchmark redis-latency \
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

DEFAULT_FILE="$RESULTS_DIR/redis_default.json"
SCHEDX_FILE="$RESULTS_DIR/redis_schedx.json"
if [ -f "$DEFAULT_FILE" ] && [ -f "$SCHEDX_FILE" ]; then
    python3 -c "
import json
with open('$DEFAULT_FILE') as f: d = json.load(f)
with open('$SCHEDX_FILE') as f: s = json.load(f)
dm = d.get('metrics', {})
sm = s.get('metrics', {})
print(f\"  {'Metric':<25} {'No SchedX':>12} {'With SchedX':>12} {'Change':>12}\")
print(f\"  {'-'*61}\")
for key in ['qps', 'p50_latency_ms', 'p95_latency_ms', 'p99_latency_ms']:
    dv = dm.get(key)
    sv = sm.get(key)
    if dv and sv:
        if 'latency' in key:
            pct = ((dv - sv) / dv) * 100
            label = 'better' if pct > 0 else 'worse'
        else:
            pct = ((sv - dv) / dv) * 100
            label = 'better' if pct > 0 else 'worse'
        print(f'  {key:<25} {dv:>12.2f} {sv:>12.2f} {pct:>+10.2f}% {label}')
"
fi
