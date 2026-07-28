#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schedx.agent.context import AgentContext
from schedx.benchmark.common import (
    cleanup_stress,
    process_ids,
    run_schedx_json,
    start_stress,
    stop_process,
    write_json,
)
from schedx.benchmark.runner import BenchmarkRunner
from schedx.controllers.scx_controller import ScxController
from schedx.llm.client import LLMClient
from schedx.scx_daemon import ScxDaemonClient
from schedx.skills.rollback_skill import RollbackSkill


def progress(step: str) -> None:
    print(f"\n{step}", file=sys.stderr, flush=True)


def preflight(require_llm: bool = False) -> dict[str, Any]:
    required = {tool: shutil.which(tool) for tool in ("wrk", "stress-ng", "sysbench")}
    nginx = subprocess.run(
        ["systemctl", "is-active", "nginx"],
        check=False,
        capture_output=True,
        text=True,
    )
    status = run_schedx_json(["status"])
    llm = LLMClient()
    return {
        "root": os.geteuid() == 0 if hasattr(os, "geteuid") else False,
        "tools": required,
        "nginx_active": nginx.stdout.strip() == "active",
        "nginx_status": nginx.stdout.strip() or nginx.stderr.strip(),
        "schedx_status": status,
        "llm": {
            "required": require_llm,
            "configured": llm.is_configured(),
            "model": llm.model if llm.is_configured() else "not configured",
            "api_base": llm.api_base,
        },
        "passed": (
            all(required.values())
            and nginx.stdout.strip() == "active"
            and (os.geteuid() == 0 if hasattr(os, "geteuid") else False)
            and (llm.is_configured() if require_llm else True)
        ),
    }


def start_daemon(run_dir: Path) -> tuple[subprocess.Popen[str] | None, Any]:
    client = ScxDaemonClient()
    if client.is_available():
        return None, None
    if not ScxController().is_available():
        return None, None
    log = (run_dir / "scx-daemon.log").open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "schedx.main", "scx-daemon", "run"],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if client.is_available():
            return process, log
        if process.poll() is not None:
            break
        time.sleep(0.25)
    log.close()
    raise RuntimeError("persistent scx daemon did not become ready")


def stop_daemon(process: subprocess.Popen[str] | None, log: Any) -> None:
    if process is not None:
        client = ScxDaemonClient()
        if client.is_available():
            try:
                client.request("shutdown")
            except (OSError, RuntimeError):
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            stop_process(process)
    if log is not None:
        log.close()


def run_canary(
    run_dir: Path,
    label: str,
    duration: int,
    stress_cpu: int,
    threshold: float,
    use_llm: bool = False,
    min_p99_improvement: float | None = None,
) -> dict[str, Any]:
    stress = start_stress(stress_cpu, duration * 3 + 20)
    try:
        time.sleep(2)
        write_json(
            run_dir / f"{label}-classify.json",
            run_schedx_json(["classify", "--top", "80"]),
        )
        optimize_args = [
            "optimize",
            "--canary-url",
            "http://127.0.0.1/",
            "--canary-duration",
            str(duration),
            "--canary-connections",
            "32",
            "--canary-threads",
            "2",
            "--canary-min-background-retention",
            str(threshold),
        ]
        if min_p99_improvement is not None:
            optimize_args.extend(
                ["--canary-min-p99-improvement", str(min_p99_improvement)]
            )
        if use_llm:
            optimize_args.extend(["--mode", "auto", "--llm-policy"])
        else:
            optimize_args.extend(["--target", "nginx", "--mode", "latency_first"])
        result = run_schedx_json(optimize_args, timeout=duration * 4 + 60)
        write_json(run_dir / f"{label}.json", result)
        return result
    finally:
        write_json(
            run_dir / f"{label}-rollback-live.json", run_schedx_json(["rollback"])
        )
        stop_process(stress)
        write_json(
            run_dir / f"{label}-rollback-final.json", run_schedx_json(["rollback"])
        )
        cleanup_stress()


def extract_agent_trace(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data", result)
    loop = data.get("agent_loop", {}) if isinstance(data, dict) else {}
    context = loop.get("context_data", {}) if isinstance(loop, dict) else {}
    raw_rollback = context.get("rollback")
    rollback = None
    if isinstance(raw_rollback, dict):
        rollback = {
            "restored": raw_rollback.get("restored", 0),
            "groups_removed": raw_rollback.get("groups_removed", 0),
            "skipped": raw_rollback.get("skipped", 0),
            "scx_entries_removed": sum(
                1
                for entry in raw_rollback.get("scx_entries", [])
                if entry.get("status") == "removed"
            ),
        }
    return {
        "decision": data.get("agent_decision", context.get("agent_decision", {})),
        "policy_route": context.get("policy_route", {}),
        "phases_completed": loop.get("phases_completed", []),
        "final_status": loop.get("final_status", ""),
        "canary_verdict": context.get("canary_verdict", {}),
        "rollback": rollback,
    }


def build_console_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    def summarize_trace(trace: dict[str, Any]) -> dict[str, Any]:
        decision = trace.get("decision", {})
        verdict = trace.get("canary_verdict", {})
        deltas = verdict.get("deltas", {})
        return {
            "source": decision.get("source"),
            "expert": decision.get("expert_id"),
            "mode": decision.get("mode"),
            "target": decision.get("target"),
            "final_status": trace.get("final_status"),
            "verdict": verdict.get("status"),
            "reasons": verdict.get("reasons", []),
            "p99_change_percent": deltas.get("p99_percent"),
            "rps_change_percent": deltas.get("requests_per_sec_percent"),
            "background_retention_percent": deltas.get(
                "background_retention_percent"
            ),
            "rollback": trace.get("rollback"),
        }

    trace = manifest.get("agent_trace", {})
    report = manifest.get("report", {})
    return {
        "status": manifest.get("status"),
        "run_dir": manifest.get("run_dir"),
        "llm_policy": manifest.get("llm_policy", False),
        "accepted_policy": summarize_trace(trace.get("accepted", {})),
        "strict_safety_gate": summarize_trace(trace.get("rejected", {})),
        "cleanup": manifest.get("cleanup", {}),
        "report": {
            "status": "ok" if report.get("returncode") == 0 else "failed",
            "path": report.get("path"),
        },
    }


def format_console_summary(summary: dict[str, Any]) -> str:
    def percent(value: object, *, lower_is_better: bool = False) -> str:
        if value is None:
            return "无有效数据"
        number = float(value)
        if abs(number) < 0.005:
            return "基本不变"
        improved = number < 0 if lower_is_better else number > 0
        arrow = "下降" if number < 0 else "上升"
        result = "改善" if improved else "回退"
        return f"{arrow} {abs(number):.2f}%（{result}）"

    def policy_name(value: object) -> str:
        return {
            "latency_first": "优先保护响应速度",
            "throughput_first": "优先提升处理能力",
            "balanced": "均衡模式",
            "isolate_background": "隔离后台干扰",
        }.get(str(value), str(value or "未知"))

    def source_name(value: object) -> str:
        return {
            "deepseek-v4": "DeepSeek 大模型",
            "explicit_cli": "固定安全规则",
            "rule_fallback": "本地规则",
        }.get(str(value), str(value or "未知"))

    def verdict_block(title: str, item: dict[str, Any]) -> list[str]:
        verdict = str(item.get("verdict", "")).upper()
        final = str(item.get("final_status", ""))
        result = "ROLLED_BACK" if final == "rolled_back" else verdict
        lines = [
            f"\n[{title}]",
            f"方案来源       : {source_name(item.get('source'))}",
            f"优化方向       : {policy_name(item.get('mode'))}",
            f"保护对象       : {item.get('target') or '未知'}",
            f"执行结果       : {result}",
            f"最慢 1% 请求延迟: {percent(item.get('p99_change_percent'), lower_is_better=True)}",
            f"每秒请求数     : {percent(item.get('rps_change_percent'))}",
            "后台任务进度   : {}".format(
                "无有效数据"
                if item.get("background_retention_percent") is None
                else f"{float(item['background_retention_percent']):.2f}%"
            ),
        ]
        if item.get("reasons"):
            reason_names = {
                "insufficient_p99_improvement": "未达到严格的延迟改善目标",
                "throughput_regression": "每秒请求数下降超过安全范围",
                "background_progress_regression": "后台任务进度下降超过安全范围",
            }
            lines.append(
                "拒绝原因       : "
                + "，".join(reason_names.get(reason, reason) for reason in item["reasons"])
            )
        rollback = item.get("rollback")
        if rollback:
            lines.append(
                "回滚结果       : 恢复 {} 项，删除 {} 个资源组和 {} 个调度策略".format(
                    rollback.get("restored", 0),
                    rollback.get("groups_removed", 0),
                    rollback.get("scx_entries_removed", 0),
                )
            )
        return lines

    cleanup = summary.get("cleanup", {})
    lines = [
        "\n=== SchedX-Agent 运行摘要 ===",
        f"总体状态       : {str(summary.get('status', '')).upper()}",
        f"结果目录       : {summary.get('run_dir')}",
    ]
    lines.extend(verdict_block("常规小范围试运行", summary["accepted_policy"]))
    lines.extend(verdict_block("严格安全检查", summary["strict_safety_gate"]))
    lines.extend(
        [
            "\n[环境恢复]",
            f"后台干扰已停止 : {not cleanup.get('stress_ng_running', True)}",
            f"资源控制已清理 : {not cleanup.get('cgroup_base_exists', True)}",
            f"调度器状态     : {cleanup.get('sched_ext_state')}",
            f"报告文件       : {summary.get('report', {}).get('path')}",
        ]
    )
    return "\n".join(lines)


def generate_report(results: Path, run_dir: Path, output: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/generate_competition_report.py",
            "--results",
            str(results),
            "--demo-run",
            str(run_dir),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "path": str(output),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    duration = 20 if args.formal else args.duration
    repeats = 3 if args.formal else args.repeats
    run_dir = args.output / time.strftime("%Y-%m-%d_%H-%M-%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    check = preflight(args.llm_policy)
    write_json(run_dir / "preflight.json", check)
    if not check["passed"]:
        result = {
            "status": "failed",
            "reason": "preflight_failed",
            "run_dir": str(run_dir),
        }
        write_json(run_dir / "manifest.json", result)
        return result
    if ScxController().state() == "enabled":
        result = {
            "status": "failed",
            "reason": "stop the existing sched_ext scheduler before running a clean demo",
            "run_dir": str(run_dir),
        }
        write_json(run_dir / "manifest.json", result)
        return result

    daemon_process: subprocess.Popen[str] | None = None
    daemon_log = None
    manifest: dict[str, Any] = {
        "status": "running",
        "run_dir": str(run_dir),
        "duration": duration,
        "repeats": repeats,
        "llm_policy": args.llm_policy,
    }
    try:
        progress("[1/4] 对比四种资源管控方案")
        ablation = BenchmarkRunner().run(
            "nginx-ablation",
            run_dir / "nginx-ablation",
            duration=duration,
            connections=args.connections,
            threads=args.threads,
            repeats=repeats,
            stress_cpu=args.stress_cpu,
            warmup=2,
        )
        manifest["nginx_ablation"] = ablation
        write_json(run_dir / "nginx-ablation-result.json", ablation)

        progress("[2/4] 验证第二类任务：批处理计算")
        batch = BenchmarkRunner().run(
            "batch-throughput",
            run_dir / "batch-throughput",
            duration=duration,
            threads=args.batch_threads,
            repeats=repeats,
            stress_cpu=args.stress_cpu,
            warmup=2,
        )
        manifest["batch_throughput"] = batch
        write_json(run_dir / "batch-throughput-result.json", batch)

        daemon_process, daemon_log = start_daemon(run_dir)
        write_json(
            run_dir / "daemon-active.json", run_schedx_json(["scx-daemon", "status"])
        )
        progress("[3/4] DeepSeek 方案：常规小范围试运行")
        manifest["accepted_canary"] = run_canary(
            run_dir,
            "canary-accepted",
            max(3, min(duration, 5)),
            args.stress_cpu,
            0.25,
            use_llm=args.llm_policy,
        )
        progress("[4/4] 严格安全检查：不达标就自动恢复")
        manifest["rollback_canary"] = run_canary(
            run_dir,
            "canary-rollback",
            max(3, min(duration, 5)),
            args.stress_cpu,
            0.25,
            use_llm=False,
            min_p99_improvement=101.0,
        )
        manifest["agent_trace"] = {
            "accepted": extract_agent_trace(manifest["accepted_canary"]),
            "rejected": extract_agent_trace(manifest["rollback_canary"]),
        }
        write_json(run_dir / "agent-trace.json", manifest["agent_trace"])
        manifest["status"] = (
            "ok"
            if ablation.get("status") == "ok" and batch.get("status") == "ok"
            else "failed"
        )
    except Exception as exc:
        manifest.update({"status": "failed", "error": str(exc)})
    finally:
        progress("[清理] 停止后台干扰并恢复默认状态")
        cleanup_stress()
        manifest["final_rollback"] = run_schedx_json(["rollback"])
        stop_daemon(daemon_process, daemon_log)
        time.sleep(1)
        manifest["final_status"] = run_schedx_json(["status"])
        manifest["cleanup"] = {
            "stress_ng_running": bool(process_ids("stress-ng", "stress-ng-cpu")),
            "cgroup_base_exists": Path("/sys/fs/cgroup/schedx").exists(),
            "sched_ext_state": ScxController().state(),
        }

    write_json(run_dir / "manifest.json", manifest)
    write_json(args.output / "latest.json", {"run_dir": str(run_dir)})
    report = generate_report(args.output.parent, run_dir, args.report)
    manifest["report"] = report
    write_json(run_dir / "manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the SchedX competition demo end to end"
    )
    parser.add_argument("--output", type=Path, default=Path("results/competition-demo"))
    parser.add_argument(
        "--report", type=Path, default=Path("reports/competition-final.md")
    )
    parser.add_argument("--duration", type=int, default=6)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--connections", type=int, default=32)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--batch-threads", type=int, default=4)
    parser.add_argument("--stress-cpu", type=int, default=4)
    parser.add_argument("--formal", action="store_true", default=False)
    parser.add_argument(
        "--llm-policy",
        action="store_true",
        default=False,
        help="Require a configured LLM and use it for autonomous canary policy decisions",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        default=False,
        help="Print the Agent decision, verification, rollback, and cleanup summary only",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    result = run(args)
    if args.compact:
        print(format_console_summary(build_console_summary(result)))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
