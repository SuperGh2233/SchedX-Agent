#!/usr/bin/env bash
# run_nginx_experiment.sh - Nginx latency protection experiment
#
# Agent autonomously decides strategy based on workload classification.
#
# Usage: sudo bash scripts/run_nginx_experiment.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

RESULTS_DIR="${SCHEDX_OUTPUT:-results}"
DURATION="${SCHEDX_DURATION:-10}"
URL="${SCHEDX_NGINX_URL:-http://127.0.0.1/}"
STRESS_CPU="${SCHEDX_STRESS_CPU:-2}"

mkdir -p "$RESULTS_DIR"

cleanup() {
    pkill -f "stress-ng" 2>/dev/null || true
    python3 -m schedx rollback 2>/dev/null || true
}
trap cleanup EXIT

echo "============================================"
echo "  SchedX-Agent Nginx Experiment"
echo "  (Agent auto-decides strategy)"
echo "============================================"
echo ""

# Phase 1: stress-ng interference, NO optimization
echo "[1/2] Running wrk with stress-ng (no optimization)..."
stress-ng --cpu "$STRESS_CPU" --timeout "$((DURATION + 10))s" --metrics-brief &
STRESS_PID=$!
sleep 2

python3 -m schedx benchmark nginx-latency \
    --variant default \
    --duration "$DURATION" \
    --url "$URL" \
    --output "$RESULTS_DIR"

kill $STRESS_PID 2>/dev/null || true
wait $STRESS_PID 2>/dev/null || true
pkill -f "stress-ng" 2>/dev/null || true
sleep 1
echo ""

# Phase 2: Agent auto-decides and applies optimization
echo "[2/2] Agent optimizing for nginx + running wrk..."
stress-ng --cpu "$STRESS_CPU" --timeout "$((DURATION + 10))s" --metrics-brief &
STRESS_PID=$!
sleep 2
python3 -m schedx optimize --target nginx
sleep 1

python3 -m schedx benchmark nginx-latency \
    --variant schedx \
    --duration "$DURATION" \
    --url "$URL" \
    --output "$RESULTS_DIR"

kill $STRESS_PID 2>/dev/null || true
wait $STRESS_PID 2>/dev/null || true
echo ""

echo "============================================"
echo "  Results"
echo "============================================"
echo ""

DEFAULT_FILE="$RESULTS_DIR/nginx_default.json"
SCHEDX_FILE="$RESULTS_DIR/nginx_schedx.json"
if [ -f "$DEFAULT_FILE" ] && [ -f "$SCHEDX_FILE" ]; then
    python3 -c "
import json
with open('$DEFAULT_FILE') as f: d = json.load(f)
with open('$SCHEDX_FILE') as f: s = json.load(f)
dm = d.get('metrics', {})
sm = s.get('metrics', {})
print(f\"  {'Metric':<25} {'No SchedX':>12} {'With SchedX':>12} {'Change':>12}\")
print(f\"  {'-'*61}\")
for key in ['requests_per_sec', 'latency_avg_ms', 'p99_ms']:
    dv = dm.get(key)
    sv = sm.get(key)
    if dv and sv:
        if 'latency' in key or 'p99' in key:
            pct = ((dv - sv) / dv) * 100
            label = 'better' if pct > 0 else 'worse'
        else:
            pct = ((sv - dv) / dv) * 100
            label = 'better' if pct > 0 else 'worse'
        print(f'  {key:<25} {dv:>12.2f} {sv:>12.2f} {pct:>+10.2f}% {label}')
"
fi
