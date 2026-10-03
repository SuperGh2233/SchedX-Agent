#!/usr/bin/env python3
"""Generate a competition report from reproducible SchedX artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def latest_summary(root: Path) -> dict[str, Any]:
    candidates = sorted(
        root.glob("**/summary.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return read_json(candidates[0]) if candidates else {}


def latest_native_summary(results: Path) -> dict[str, Any]:
    candidates = sorted(
        (
            path
            for path in results.glob("**/summary.json")
            if "native-scx" in str(path.parent).lower()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    payloads = [read_json(path) for path in candidates]
    for payload in payloads:
        comparison = payload.get("comparison", {})
        if comparison.get("valid_for_performance_claims") is True:
            return payload
    return payloads[0] if payloads else {}


def locate_demo(results: Path, demo_run: Path | None) -> Path | None:
    if demo_run is not None:
        return demo_run
    latest = read_json(results / "competition-demo" / "latest.json")
    value = latest.get("run_dir")
    if value:
        return Path(str(value))
    legacy = results / "competition-demo"
    return legacy if legacy.exists() else None


def extract(results: Path, demo_run: Path | None = None) -> dict[str, Any]:
    run_dir = locate_demo(results, demo_run)
    manifest = read_json(run_dir / "manifest.json") if run_dir else {}
    ablation = _nested_summary(manifest.get("nginx_ablation"))
    batch = _nested_summary(manifest.get("batch_throughput"))
    if not ablation:
        ablation = latest_summary(results / "nginx-ablation")
    formal_batch = latest_summary(results / "batch-throughput")
    if formal_batch.get("phases", {}).get("schedx", {}).get("valid_for_claims"):
        batch = formal_batch
    elif not batch:
        batch = formal_batch

    native = latest_native_summary(results)
    scx_comparison = latest_summary(results / "scx-compare-formal")
    redis = latest_summary(results / "redis-formal-final")
    llm = read_json(results / "llm-policy-comparison" / "summary.json")
    return {
        "run_dir": str(run_dir) if run_dir else "",
        "manifest": manifest,
        "ablation": ablation,
        "batch": batch,
        "accepted_canary": _canary(manifest.get("accepted_canary")),
        "rollback_canary": _canary(manifest.get("rollback_canary")),
        "native": native,
        "scx_comparison": scx_comparison,
        "redis": redis,
        "llm": llm,
    }


def _nested_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    summary = value.get("summary")
    return summary if isinstance(summary, dict) else {}


def _canary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    data = value.get("data", {})
    loop = data.get("agent_loop", {}) if isinstance(data, dict) else {}
    context = loop.get("context_data", {}) if isinstance(loop, dict) else {}
    return {
        "returncode": value.get("returncode"),
        "final_status": loop.get("final_status"),
        "verdict": context.get("canary_verdict", {}),
        "rollback": context.get("rollback"),
    }


def generate(
    results: Path,
    output: Path,
    demo_run: Path | None = None,
) -> Path:
    data = extract(results, demo_run)
    output.parent.mkdir(parents=True, exist_ok=True)
    status = data["manifest"].get("final_status", {}).get("data", {})
    environment = status if isinstance(status, dict) else {}
    lines = [
        "# SchedX-Agent Competition Report",
        "",
        "## 1. Executive Summary",
        "",
        "SchedX-Agent is an adaptive Linux resource-control Agent for mixed workloads.",
        "It combines workload sensing, allowlisted expert routing, native sched_ext,",
        "cgroup v2 enforcement, real SLO canaries, and automatic rollback.",
        "",
        f"- Demo artifact: `{data['run_dir'] or 'not available'}`",
        f"- Demo status: `{data['manifest'].get('status', 'not available')}`",
        f"- Agent version: `{environment.get('schedx', 'not available')}`",
        f"- Kernel: `{environment.get('platform', 'not available')}`",
        f"- Runtime mode: `{environment.get('mode', 'not available')}`",
        "",
        "## 2. Architecture",
        "",
        "```mermaid",
        "flowchart LR",
        "  P[procfs / PSI / cgroup] --> C[Workload classifier]",
        "  C --> R[Expert policy router]",
        "  R --> B[Baseline SLO canary]",
        "  B --> S[native sched_ext]",
        "  B --> G[cgroup v2]",
        "  S --> V[Candidate verifier]",
        "  G --> V",
        "  V -->|accepted| H[Policy outcome history]",
        "  V -->|rejected| X[Automatic rollback]",
        "```",
        "",
        "## 3. Four-Way Nginx Ablation",
        "",
        *_ablation_table(data["ablation"]),
        "",
        *_ablation_charts(data["ablation"]),
        "",
        "The ablation isolates default Linux scheduling, cgroup-only control,",
        "native scx-only control, and the combined adaptive Agent.",
        "Only rows meeting the background-progress floor are valid performance claims.",
        "",
        "## 4. Batch Throughput Scenario",
        "",
        *_batch_table(data["batch"]),
        "",
        *_batch_chart(data["batch"]),
        "",
        "The SchedX phase identifies a live sysbench workload and applies the",
        "throughput expert while CPU interference remains active.",
        "",
        "## 5. Redis Latency Scenario",
        "",
        *_redis_table(data["redis"]),
        "",
        "The fixed-work Redis experiment rotates phase order across five repeats",
        "and reports small-sample Student-t 95% confidence intervals.",
        "",
        "## 6. Canary And Rollback Evidence",
        "",
        *_canary_lines("Accepted gate", data["accepted_canary"]),
        *_canary_lines("Strict rollback gate", data["rollback_canary"]),
        "",
        "## 7. Native sched_ext Scheduler Comparison",
        "",
        *_scx_comparison_table(data["scx_comparison"]),
        "",
        "The same repeated harness compares the default scheduler with allowlisted",
        "upstream scx examples and SchedX's task-policy scheduler. Capability labels",
        "prevent lifecycle-only schedulers from being presented as adaptive Agents.",
        "",
        "## 8. Existing Native sched_ext Evidence",
        "",
        *_native_lines(data["native"]),
        "",
        "## 9. Real eBPF Evidence",
        "",
        "The Agent loop loads and attaches scheduler, network, resource, and security",
        "BPF programs, updates allowlisted maps, collects counters for verification,",
        "and removes pinned hooks through the same rollback path.",
        "",
        "## 10. Optional LLM Evidence",
        "",
        *_llm_lines(data["llm"]),
        "",
        "## 11. Reproduction",
        "",
        "```bash",
        "sudo bash scripts/run_competition_demo.sh",
        "sudo bash scripts/run_competition_demo.sh --formal",
        "sudo schedx benchmark scx-compare --schedulers scx_simple,scx_qmap,scx_flatcg,scx_agent --duration 20 --repeats 5",
        "sudo schedx benchmark redis --duration 10 --repeats 5 --stress-cpu 4 --output results/redis-formal-final",
        "python3 -m pytest -q",
        "```",
        "",
        "The short demo is presentation evidence. Formal claims should use",
        "the repeated `--formal` run and retain all raw wrk/sysbench outputs.",
        "",
        "## 12. Limitations",
        "",
        "- Formal evidence covers nginx, Redis, and sysbench under CPU contention; it should not be generalized to every workload class.",
        "- Same-host load generation can introduce client-side contention; an external load generator is preferable for final measurements.",
        "- Network and security eBPF programs currently enforce bounded map policies and audit evidence; full production firewall or mandatory-access-control semantics remain future work.",
        "- LLM policy planning is optional; safety and execution do not depend on model availability.",
    ]
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def _ablation_table(summary: dict[str, Any]) -> list[str]:
    lines = [
        "| Phase | Mean RPS | Mean P99 (ms) | RPS gain | P99 reduction | Background retention | Valid |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    phases = summary.get("phases", {})
    for name in ("default", "cgroup_only", "scx_only", "agent_combined"):
        item = phases.get(name, {})
        lines.append(
            f"| {name} | {num(item.get('mean_requests_per_sec'))} | {num(item.get('mean_p99_ms'))} | "
            f"{pct(item.get('rps_gain_vs_default_percent'))} | "
            f"{pct(item.get('p99_reduction_vs_default_percent'))} | "
            f"{pct(item.get('background_retention_percent'))} | "
            f"{item.get('valid_for_claims', 'n/a')} |"
        )
    return lines


def _batch_table(summary: dict[str, Any]) -> list[str]:
    lines = [
        "| Phase | Mean events/s | P95 latency (ms) | Gain vs interference | Background progress | Valid |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    phases = summary.get("phases", {})
    for name in ("baseline", "interference", "schedx"):
        item = phases.get(name, {})
        lines.append(
            f"| {name} | {num(item.get('mean_events_per_second'))} | "
            f"{num(item.get('mean_latency_p95_ms'))} | "
            f"{pct(item.get('throughput_gain_vs_interference_percent'))} | "
            f"{pct(item.get('background_retention_percent'))} | "
            f"{item.get('valid_for_claims', 'n/a')} |"
        )
    return lines


def _redis_table(summary: dict[str, Any]) -> list[str]:
    lines = [
        "| Phase | Mean QPS | QPS 95% CI | Mean P99 (ms) | P99 95% CI | Background retention | Valid |",
        "| --- | ---: | --- | ---: | --- | ---: | --- |",
    ]
    phases = summary.get("phases", {})
    for name in ("baseline", "interference", "schedx"):
        item = phases.get(name, {})
        qps = item.get("requests_per_sec", {})
        p99 = item.get("p99_ms", {})
        lines.append(
            f"| {name} | {num(qps.get('mean'))} | {_ci(qps.get('ci95'))} | "
            f"{num(p99.get('mean'))} | {_ci(p99.get('ci95'))} | "
            f"{pct(item.get('background_retention_percent'))} | "
            f"{item.get('valid_for_claims', 'n/a')} |"
        )
    lines.extend(
        [
            "",
            f"- QPS recovery vs interference: {pct(summary.get('schedx_qps_gain_vs_interference_percent'))}",
            f"- P99 reduction vs interference: {pct(summary.get('schedx_p99_reduction_vs_interference_percent'))}",
        ]
    )
    return lines


def _ci(value: Any) -> str:
    if not isinstance(value, list) or len(value) != 2:
        return "n/a"
    return f"[{num(value[0])}, {num(value[1])}]"


def _scx_comparison_table(summary: dict[str, Any]) -> list[str]:
    lines = [
        "| Scheduler | Capability | Mean RPS | Mean P99 (ms) | RPS gain | P99 reduction | Background retention | Valid |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for name, item in summary.get("schedulers", {}).items():
        stats = item.get("statistics", {})
        comparison = item.get("comparison_vs_default", {})
        fairness = item.get("fairness", {})
        lines.append(
            f"| {name} | {item.get('policy_capability', 'n/a')} | "
            f"{num(stats.get('requests_per_sec', {}).get('mean'))} | "
            f"{num(stats.get('p99_ms', {}).get('mean'))} | "
            f"{pct(comparison.get('rps_gain_percent'))} | "
            f"{pct(comparison.get('p99_reduction_percent'))} | "
            f"{pct(item.get('background_retention_percent'))} | "
            f"{fairness.get('valid_for_claims', 'n/a')} |"
        )
    return lines


def _ablation_charts(summary: dict[str, Any]) -> list[str]:
    phases = summary.get("phases", {})
    names = ("default", "cgroup_only", "scx_only", "agent_combined")
    rps = [_number(phases.get(name, {}).get("mean_requests_per_sec")) for name in names]
    p99 = [_number(phases.get(name, {}).get("mean_p99_ms")) for name in names]
    lines: list[str] = []
    if any(rps):
        lines.extend(
            [
                "### RPS",
                "",
                "```mermaid",
                "xychart-beta",
                '  x-axis ["default", "cgroup", "scx", "agent"]',
                f'  y-axis "Requests/sec" 0 --> {max(rps) * 1.1:.2f}',
                f"  bar [{', '.join(f'{value:.2f}' for value in rps)}]",
                "```",
            ]
        )
    if any(p99):
        lines.extend(
            [
                "",
                "### P99 Latency",
                "",
                "```mermaid",
                "xychart-beta",
                '  x-axis ["default", "cgroup", "scx", "agent"]',
                f'  y-axis "P99 ms" 0 --> {max(p99) * 1.1:.2f}',
                f"  bar [{', '.join(f'{value:.2f}' for value in p99)}]",
                "```",
            ]
        )
    return lines


def _batch_chart(summary: dict[str, Any]) -> list[str]:
    phases = summary.get("phases", {})
    names = ("baseline", "interference", "schedx")
    values = [
        _number(phases.get(name, {}).get("mean_events_per_second")) for name in names
    ]
    if not any(values):
        return []
    return [
        "### Batch Throughput",
        "",
        "```mermaid",
        "xychart-beta",
        '  x-axis ["baseline", "interference", "schedx"]',
        f'  y-axis "Events/sec" 0 --> {max(values) * 1.1:.2f}',
        f"  bar [{', '.join(f'{value:.2f}' for value in values)}]",
        "```",
    ]


def _canary_lines(label: str, canary: dict[str, Any]) -> list[str]:
    verdict = canary.get("verdict", {})
    deltas = verdict.get("deltas", {}) if isinstance(verdict, dict) else {}
    rollback = canary.get("rollback")
    return [
        f"### {label}",
        "",
        f"- Final status: `{canary.get('final_status', 'n/a')}`",
        f"- Verdict: `{verdict.get('status', 'n/a') if isinstance(verdict, dict) else 'n/a'}`",
        f"- RPS change: {pct(deltas.get('requests_per_sec_percent'))}",
        f"- P99 delta (negative is better): {pct(deltas.get('p99_percent'))}",
        f"- Background retention: {pct(deltas.get('background_retention_percent'))}",
        f"- Rollback evidence present: `{isinstance(rollback, dict)}`",
        "",
    ]


def _native_lines(native: dict[str, Any]) -> list[str]:
    comparison = native.get("comparison", {})
    return [
        f"- RPS gain: {pct(comparison.get('rps_gain_percent'))}",
        f"- P99 reduction: {pct(comparison.get('p99_reduction_percent'))}",
        f"- Background retention: {pct(comparison.get('background_cpu_retention_percent'))}",
        f"- Valid for claims: `{comparison.get('valid_for_performance_claims', 'n/a')}`",
    ]


def _llm_lines(llm: dict[str, Any]) -> list[str]:
    decision = llm.get("llm_decision", {})
    comparison = llm.get("comparison", {}).get("llm", {})
    return [
        f"- Source: `{llm.get('llm_source', 'not configured')}`",
        f"- Mode: `{decision.get('mode', 'n/a')}`",
        f"- Target: `{decision.get('target', 'n/a')}`",
        f"- RPS gain: {pct(comparison.get('rps_gain_percent'))}",
        f"- P99 reduction: {pct(comparison.get('p99_reduction_percent'))}",
    ]


def pct(value: Any) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def num(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--demo-run", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("reports/competition-final.md")
    )
    args = parser.parse_args()
    path = generate(args.results, args.output, args.demo_run)
    print(json.dumps({"status": "ok", "report": str(path)}, indent=2))


if __name__ == "__main__":
    main()
