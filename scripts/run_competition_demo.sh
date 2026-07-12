#!/usr/bin/env bash
set -euo pipefail

OUT="${1:-results/competition-demo}"
mkdir -p "$OUT"

echo "== SchedX status =="
python3 -m schedx status | tee "$OUT/status.json"

echo "== DeepSeek V4 policy plan =="
python3 -m schedx llm-plan | tee "$OUT/llm-plan.json"

echo "== Persistent sched_ext daemon =="
python3 -m schedx scx-daemon status | tee "$OUT/scx-daemon-status.json"
python3 -m schedx scx-daemon cleanup-metrics | tee "$OUT/scx-cleanup-metrics.json"
python3 -m schedx scx-daemon metrics | tee "$OUT/scx-metrics-before.json"

echo "== Agent tool call under daemon sched_ext =="
python3 -m schedx tool-run --agent-id demo --intent background -- \
  stress-ng --cpu 2 --timeout 3s 2> "$OUT/tool-run.stderr" | tee "$OUT/tool-run.stdout"
tail -n 20 "$OUT/tool-run.stderr"

echo "== cgroup policy inheritance =="
python3 scripts/verify_scx_cgroup_inheritance.py | tee "$OUT/cgroup-inheritance.json"

echo "== closed-loop convergence =="
python3 scripts/verify_closed_loop_convergence.py | tee "$OUT/closed-loop-convergence.json"

if [[ "${SCHEDX_DEMO_SKIP_MULTI_AGENT:-0}" != "1" ]]; then
  echo "== multi-Agent LLM scheduling =="
  python3 scripts/run_multi_agent_llm_experiment.py \
    --agents "${SCHEDX_DEMO_AGENTS:-6}" \
    --duration "${SCHEDX_DEMO_MULTI_AGENT_DURATION:-8}" \
    --output "$OUT/multi-agent-llm" | tee "$OUT/multi-agent-llm.json"
fi

echo "== final daemon status =="
python3 -m schedx scx-daemon status | tee "$OUT/final-status.json"
python3 scripts/generate_competition_report.py --results results --output reports/competition-final.md
echo "demo artifacts: $OUT"
