from __future__ import annotations

import argparse
import json
import os
import platform
from dataclasses import asdict
from dataclasses import is_dataclass
from pathlib import Path
from typing import Any

from schedx.agent.context import AgentContext
from schedx.agent.loop import AgentLoop
from schedx.agent.executor import SafeActionExecutor
from schedx.benchmark.runner import BenchmarkRunner
from schedx.controllers.cgroup_controller import CgroupController
from schedx.controllers.scx_controller import ScxController
from schedx.policies.classifier import WorkloadClassifier
from schedx.policies.planner import PolicyPlanner
from schedx.probes.cgroup_probe import CgroupProbe
from schedx.probes.procfs_probe import ProcfsProbe
from schedx.report.report_generator import ReportGenerator
from schedx.report.repeat_summary import RepeatSummaryGenerator
from schedx.skills.act_skill import ActSkill
from schedx.skills.analyze_skill import AnalyzeSkill
from schedx.skills.policy_skill import PolicySkill
from schedx.skills.probe_skill import ProbeSkill
from schedx.skills.report_skill import ReportSkill
from schedx.skills.rollback_skill import RollbackSkill
from schedx.skills.verify_skill import VerifySkill
from schedx.tool_runner import PROFILES, ToolCallRunner, emit_tool_result
from schedx.scx_daemon import DEFAULT_SOCKET, ScxDaemonClient, serve_scx_daemon


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def dry_run_from_args(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "dry_run", False))


def build_context(args: argparse.Namespace) -> AgentContext:
    return AgentContext(dry_run=dry_run_from_args(args))


def cmd_status(args: argparse.Namespace) -> int:
    cgroup = CgroupProbe()
    from schedx.probes.cpu_topology_probe import CpuTopologyProbe
    from schedx.llm.client import LLMClient
    topo = CpuTopologyProbe().get_topology()
    llm = LLMClient()
    print_json(
        {
            "schedx": "0.3.0",
            "platform": platform.platform(),
            "cgroup_v2": cgroup.is_v2(),
            "cgroup_controllers": cgroup.controllers(),
            "cgroup_cpu_stat": cgroup.cpu_stat(),
            "cpu_topology": topo,
            "sched_ext": ScxController().status(),
            "scx_daemon_available": ScxDaemonClient().is_available(),
            "mode": "sched_ext-emulated (cpuset+affinity+nice)" if not ScxController().is_available() else "sched_ext-native",
            "llm_configured": llm.is_configured(),
            "llm_model": llm.model if llm.is_configured() else "not configured",
        }
    )
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    context = build_context(args)
    context.data["interval"] = args.interval
    context.data["top"] = args.top
    result = ProbeSkill().run(context)
    print_json(result.data if result.ok else {"error": result.message})
    return 0 if result.ok else 1


def cmd_classify(args: argparse.Namespace) -> int:
    context = build_context(args)
    context.data["interval"] = args.interval
    context.data["top"] = args.top

    probe_result = ProbeSkill().run(context)
    if not probe_result.ok:
        print_json({"error": f"probe failed: {probe_result.message}"})
        return 1

    analyze_result = AnalyzeSkill().run(context)
    if not analyze_result.ok:
        print_json({"error": f"classification failed: {analyze_result.message}"})
        return 1

    from schedx.agent.decision import DecisionEngine
    from schedx.probes.cpu_topology_probe import CpuTopologyProbe
    engine = DecisionEngine()
    classification = context.data.get("classification", {})
    pressure = context.data.get("snapshot", {}).get("pressure", {})
    topology = CpuTopologyProbe().get_topology()
    decision = engine.decide(classification, pressure, topology)

    result_data = {
        "classification": classification,
        "agent_decision": {
            "mode": decision.mode,
            "target": decision.target,
            "reason": decision.reason,
            "confidence": decision.confidence,
            "parameters": decision.parameters,
        },
    }

    if args.llm:
        from schedx.skills.llm_analyze_skill import LlmAnalyzeSkill
        context.data["topology"] = topology
        llm_result = LlmAnalyzeSkill().run(context)
        if llm_result.ok:
            result_data["llm_analysis"] = llm_result.data.get("analysis", "")

    print_json(result_data)
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    context = build_context(args)
    context.data["interval"] = args.interval
    context.data["top"] = args.top

    auto_mode = getattr(args, "mode", "auto") == "auto"
    if not auto_mode:
        context.data["mode"] = args.mode
    if args.target:
        context.data["target"] = args.target
    context.data["llm_policy_enabled"] = args.llm_policy

    loop = AgentLoop(context, max_iterations=10)
    report = loop.run(initial_phase="probe")

    print_json({
        "dry_run": dry_run_from_args(args),
        "auto_mode": auto_mode,
        "agent_decision": context.data.get("agent_decision"),
        "agent_loop": report,
    })

    context.save_session()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    context = AgentContext(dry_run=False)
    context.data["llm_policy_enabled"] = args.llm_policy
    loop = AgentLoop(context, max_iterations=50)
    print(f"SchedX Agent starting continuous mode (interval={args.interval}s, max_rounds={args.max_rounds})")
    print("Press Ctrl+C to stop.")
    loop.run_continuous(interval=args.interval, max_rounds=args.max_rounds)
    return 0


def cmd_llm_analyze(args: argparse.Namespace) -> int:
    context = build_context(args)
    context.data["interval"] = 0.2
    context.data["top"] = 50

    probe_result = ProbeSkill().run(context)
    if not probe_result.ok:
        print_json({"error": f"probe failed: {probe_result.message}"})
        return 1

    analyze_result = AnalyzeSkill().run(context)
    if not analyze_result.ok:
        print_json({"error": f"classify failed: {analyze_result.message}"})
        return 1

    from schedx.probes.cpu_topology_probe import CpuTopologyProbe
    context.data["topology"] = CpuTopologyProbe().get_topology()

    from schedx.skills.llm_analyze_skill import LlmAnalyzeSkill
    result = LlmAnalyzeSkill().run(context)
    if result.ok:
        print(result.data.get("analysis", ""))
    else:
        print_json({"error": result.message})
        return 1
    return 0


def cmd_llm_plan(args: argparse.Namespace) -> int:
    context = build_context(args)
    context.data["interval"] = args.interval
    context.data["top"] = args.top
    probe_result = ProbeSkill().run(context)
    analyze_result = AnalyzeSkill().run(context) if probe_result.ok else probe_result
    if not analyze_result.ok:
        print_json({"error": analyze_result.message})
        return 1
    from schedx.probes.cpu_topology_probe import CpuTopologyProbe
    from schedx.skills.llm_policy_skill import LlmPolicySkill

    context.data["topology"] = CpuTopologyProbe().get_topology()
    result = LlmPolicySkill().run(context)
    print_json(result.data)
    return 0 if result.ok else 1


def cmd_llm_report(args: argparse.Namespace) -> int:
    context = build_context(args)
    context.data["results_path"] = args.results

    from schedx.skills.llm_report_skill import LlmReportSkill
    result = LlmReportSkill().run(context)
    if result.ok:
        report = result.data.get("report", "")
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        print(f"LLM report written to {output_path}")
    else:
        print_json({"error": result.message})
        return 1
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    context = build_context(args)
    from schedx.controllers.cgroup_controller import CgroupController
    cgroup = CgroupController(dry_run=context.dry_run)
    group = args.group or f"manual-{args.pid}"
    cgroup.add_pid(group, args.pid)
    if args.cpu_weight is not None:
        cgroup.set_cpu_weight(group, args.cpu_weight)
    if args.cpu_max is not None:
        cgroup.set_cpu_max(group, args.cpu_max)
    print_json({"dry_run": dry_run_from_args(args), "group": group, "pid": args.pid, "status": "ok"})
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    context = build_context(args)
    result = RollbackSkill().run(context)
    print_json(result.data if result.ok else {"error": result.message})
    return 0 if result.ok else 1


def cmd_benchmark(args: argparse.Namespace) -> int:
    print_json(
        BenchmarkRunner().run(
            args.name,
            Path(args.output),
            variant=args.variant,
            duration=args.duration,
            url=args.url,
            connections=args.connections,
            threads=args.threads,
            repeats=args.repeats,
            stress_cpu=args.stress_cpu,
        )
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    results_dir = Path(args.results)
    output_path = Path(args.output)
    report_path = ReportGenerator().generate(results_dir, output_path)
    print_json({"report": str(report_path), "status": "ok"})
    return 0


def cmd_report_repeats(args: argparse.Namespace) -> int:
    summary = RepeatSummaryGenerator().generate(Path(args.results), Path(args.output))
    print_json(summary)
    return 0


def cmd_tool_run(args: argparse.Namespace) -> int:
    command = list(args.tool_command)
    if command and command[0] == "--":
        command = command[1:]
    overrides = {
        key: value
        for key, value in {
            "memory_high": args.memory_high,
            "memory_max": args.memory_max,
            "cpu_weight": args.cpu_weight,
            "cpu_max": args.cpu_max,
            "pids_max": args.pids_max,
        }.items()
        if value is not None
    }
    result = ToolCallRunner(native_scx=not args.no_scx).run(
        command,
        agent_id=args.agent_id,
        intent=args.intent,
        profile_overrides=overrides,
        resource_hint=args.resource_hint or os.environ.get("AGENT_RESOURCE_HINT", ""),
    )
    emit_tool_result(result)
    return int(result["returncode"])


def cmd_scx_daemon(args: argparse.Namespace) -> int:
    socket_path = Path(args.socket)
    client = ScxDaemonClient(socket_path)
    if args.daemon_action == "run":
        serve_scx_daemon(socket_path)
        return 0
    if args.daemon_action == "status":
        try:
            print_json(client.request("status"))
            return 0
        except Exception as exc:
            print_json({"ok": False, "error": str(exc)})
            return 1
    if args.daemon_action == "stop":
        print_json(client.request("shutdown"))
        return 0
    if args.daemon_action == "stats":
        print_json(client.request("stats"))
        return 0
    if args.daemon_action == "metrics":
        print_json(client.request("cgroup_metrics"))
        return 0
    if args.daemon_action == "cleanup-metrics":
        print_json(client.request("cleanup_metrics"))
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="schedx", description="SchedX-Agent - LLM-Powered Autonomous Resource Control Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status").set_defaults(func=cmd_status)

    probe = sub.add_parser("probe")
    probe.add_argument("--top", type=int, default=20)
    probe.add_argument("--interval", type=float, default=0.2)
    probe.set_defaults(func=cmd_probe)

    classify = sub.add_parser("classify")
    classify.add_argument("--top", type=int, default=50)
    classify.add_argument("--interval", type=float, default=0.2)
    classify.add_argument("--llm", action="store_true", default=False, help="Include LLM analysis")
    classify.set_defaults(func=cmd_classify)

    optimize = sub.add_parser("optimize")
    optimize.add_argument("--target", default="")
    optimize.add_argument(
        "--mode",
        choices=["auto", "latency_first", "throughput_first", "balanced", "isolate_background", "rollback_to_default"],
        default="auto",
    )
    optimize.add_argument("--top", type=int, default=80)
    optimize.add_argument("--interval", type=float, default=0.2)
    optimize.add_argument("--dry-run", action="store_true", default=False)
    optimize.add_argument("--llm-policy", action="store_true", default=False)
    optimize.set_defaults(func=cmd_optimize)

    run_cmd = sub.add_parser("run")
    run_cmd.add_argument("--interval", type=float, default=30, help="Seconds between rounds")
    run_cmd.add_argument("--max-rounds", type=int, default=0, help="Max rounds (0=unlimited)")
    run_cmd.add_argument("--llm-policy", action="store_true", default=False)
    run_cmd.set_defaults(func=cmd_run)

    llm_analyze = sub.add_parser("llm-analyze")
    llm_analyze.set_defaults(func=cmd_llm_analyze)

    llm_plan = sub.add_parser("llm-plan", help="Generate a validated DeepSeek scheduling policy")
    llm_plan.add_argument("--top", type=int, default=50)
    llm_plan.add_argument("--interval", type=float, default=0.2)
    llm_plan.set_defaults(func=cmd_llm_plan)

    llm_report = sub.add_parser("llm-report")
    llm_report.add_argument("--results", default="results")
    llm_report.add_argument("--output", default="reports/llm_report.md")
    llm_report.set_defaults(func=cmd_llm_report)

    control = sub.add_parser("control")
    control.add_argument("--pid", type=int, required=True)
    control.add_argument("--group")
    control.add_argument("--cpu-weight", type=int)
    control.add_argument("--cpu-max")
    control.add_argument("--dry-run", action="store_true", default=False)
    control.set_defaults(func=cmd_control)

    rollback = sub.add_parser("rollback")
    rollback.add_argument("--dry-run", action="store_true", default=False)
    rollback.set_defaults(func=cmd_rollback)

    benchmark = sub.add_parser("benchmark")
    benchmark.add_argument("name", choices=sorted(BenchmarkRunner.SUPPORTED))
    benchmark.add_argument("--output", default="results")
    benchmark.add_argument("--variant", choices=sorted(BenchmarkRunner.VARIANTS), default="default")
    benchmark.add_argument("--duration", type=int, default=10)
    benchmark.add_argument("--url", default="http://127.0.0.1/")
    benchmark.add_argument("--connections", type=int, default=64)
    benchmark.add_argument("--threads", type=int, default=4)
    benchmark.add_argument("--repeats", type=int, default=3)
    benchmark.add_argument("--stress-cpu", type=int, default=4)
    benchmark.set_defaults(func=cmd_benchmark)

    report = sub.add_parser("report")
    report.add_argument("--results", default="results")
    report.add_argument("--output", default="reports/report.md")
    report.set_defaults(func=cmd_report)

    repeat_report = sub.add_parser("report-repeats")
    repeat_report.add_argument("--results", default="results/tuned-repeats")
    repeat_report.add_argument("--output", default="reports/repeated-summary.md")
    repeat_report.set_defaults(func=cmd_report_repeats)

    tool_run = sub.add_parser("tool-run", help="Run one Agent tool call in an ephemeral cgroup")
    tool_run.add_argument("--agent-id", default="default")
    tool_run.add_argument("--intent", choices=["auto", *sorted(PROFILES)], default="auto")
    tool_run.add_argument("--memory-high")
    tool_run.add_argument("--memory-max")
    tool_run.add_argument("--cpu-weight", type=int)
    tool_run.add_argument("--cpu-max")
    tool_run.add_argument("--pids-max")
    tool_run.add_argument("--no-scx", action="store_true", default=False)
    tool_run.add_argument("--resource-hint", default="")
    tool_run.add_argument("tool_command", nargs=argparse.REMAINDER)
    tool_run.set_defaults(func=cmd_tool_run)

    scx_daemon = sub.add_parser("scx-daemon", help="Manage the persistent sched_ext owner")
    scx_daemon.add_argument(
        "daemon_action",
        choices=["run", "status", "stats", "metrics", "cleanup-metrics", "stop"],
    )
    scx_daemon.add_argument("--socket", default=str(DEFAULT_SOCKET))
    scx_daemon.set_defaults(func=cmd_scx_daemon)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


def _serialize(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
