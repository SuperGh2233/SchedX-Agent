#!/usr/bin/env bash
# run_all_experiments.sh - Run all SchedX-Agent experiments
#
# Runs nginx, redis, and batch experiments sequentially,
# then generates a combined report.
#
# Usage:
#   sudo bash scripts/run_all_experiments.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "================================================"
echo "  SchedX-Agent Full Experiment Suite"
echo "================================================"
echo ""

echo "[1/3] Nginx latency protection experiment..."
bash "$SCRIPT_DIR/run_nginx_experiment.sh"
echo ""

echo "[2/3] Redis latency protection experiment..."
bash "$SCRIPT_DIR/run_redis_experiment.sh"
echo ""

echo "[3/3] Batch throughput optimization experiment..."
bash "$SCRIPT_DIR/run_batch_experiment.sh"
echo ""

echo "================================================"
echo "  All experiments complete!"
echo "================================================"
echo ""
echo "Generating report..."
cd "$(dirname "$SCRIPT_DIR")"
python3 -m schedx report --results results --output reports/report.md
echo ""
echo "Report: reports/report.md"
echo "Results: results/"
