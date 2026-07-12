#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

REPEATS="${SCHEDX_REPEATS:-5}"
DURATION="${SCHEDX_DURATION:-30}"
STRESS_CPU="${SCHEDX_STRESS_CPU:-2}"
ROOT_OUTPUT="${SCHEDX_OUTPUT:-results/formal}"

for repeat in $(seq 1 "$REPEATS"); do
    output="$ROOT_OUTPUT/repeat-$repeat"
    mkdir -p "$output"
    echo "Running formal experiment repeat $repeat/$REPEATS"
    SCHEDX_OUTPUT="$output" SCHEDX_DURATION="$DURATION" SCHEDX_STRESS_CPU="$STRESS_CPU" \
        bash scripts/run_nginx_experiment.sh
    SCHEDX_OUTPUT="$output" SCHEDX_DURATION="$DURATION" SCHEDX_STRESS_CPU="$STRESS_CPU" \
        bash scripts/run_redis_experiment.sh
    SCHEDX_OUTPUT="$output" SCHEDX_DURATION="$DURATION" SCHEDX_STRESS_CPU="$STRESS_CPU" \
        bash scripts/run_batch_experiment.sh
done

python3 -m schedx report-repeats \
    --results "$ROOT_OUTPUT" \
    --output reports/formal-summary.md
