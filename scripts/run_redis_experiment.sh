#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
[[ -f .venv/bin/activate ]] && source .venv/bin/activate

DURATION="${SCHEDX_DURATION:-10}"
REPEATS="${SCHEDX_REPEATS:-5}"
STRESS_CPU="${SCHEDX_STRESS_CPU:-4}"
OUTPUT="${SCHEDX_OUTPUT:-results/redis-formal-final}"

systemctl is-active --quiet redis || systemctl start redis
redis-cli ping | grep -qx PONG || {
    echo "FAIL: Redis is not reachable"
    exit 1
}

python3 -m schedx benchmark redis \
    --duration "$DURATION" \
    --connections 64 \
    --threads 4 \
    --repeats "$REPEATS" \
    --stress-cpu "$STRESS_CPU" \
    --warmup 2 \
    --min-background-retention 25 \
    --output "$OUTPUT"
