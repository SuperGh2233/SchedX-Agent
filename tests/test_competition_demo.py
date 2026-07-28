import importlib.util
from pathlib import Path


def load_demo_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "run_competition_demo.py"
    )
    spec = importlib.util.spec_from_file_location("run_competition_demo", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_cli_exposes_llm_and_compact_modes():
    module = load_demo_module()

    args = module.build_parser().parse_args(["--llm-policy", "--compact"])

    assert args.llm_policy is True
    assert args.compact is True


def test_llm_canary_uses_autonomous_policy_without_explicit_target(
    tmp_path: Path, monkeypatch
):
    module = load_demo_module()
    calls = []

    monkeypatch.setattr(module, "start_stress", lambda *_: object())
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    monkeypatch.setattr(module, "write_json", lambda *_: None)
    monkeypatch.setattr(module, "stop_process", lambda *_: None)
    monkeypatch.setattr(module, "cleanup_stress", lambda: None)

    def fake_schedx(args, **kwargs):
        calls.append(args)
        return {"returncode": 0, "data": {}}

    monkeypatch.setattr(module, "run_schedx_json", fake_schedx)

    module.run_canary(tmp_path, "accepted", 3, 2, 0.25, use_llm=True)

    optimize = next(args for args in calls if args[0] == "optimize")
    assert "--llm-policy" in optimize
    assert optimize[optimize.index("--mode") + 1] == "auto"
    assert "--target" not in optimize


def test_rejection_canary_uses_post_action_p99_gate(tmp_path: Path, monkeypatch):
    module = load_demo_module()
    calls = []

    monkeypatch.setattr(module, "start_stress", lambda *_: object())
    monkeypatch.setattr(module.time, "sleep", lambda *_: None)
    monkeypatch.setattr(module, "write_json", lambda *_: None)
    monkeypatch.setattr(module, "stop_process", lambda *_: None)
    monkeypatch.setattr(module, "cleanup_stress", lambda: None)

    def fake_schedx(args, **kwargs):
        calls.append(args)
        return {"returncode": 0, "data": {}}

    monkeypatch.setattr(module, "run_schedx_json", fake_schedx)

    module.run_canary(
        tmp_path,
        "rejected",
        3,
        2,
        0.25,
        use_llm=True,
        min_p99_improvement=99.9,
    )

    optimize = next(args for args in calls if args[0] == "optimize")
    gate = optimize.index("--canary-min-p99-improvement")
    assert optimize[gate + 1] == "99.9"


def test_agent_trace_surfaces_decision_route_verdict_and_rollback():
    module = load_demo_module()
    result = {
        "data": {
            "agent_decision": {"source": "deepseek-v4", "mode": "latency_first"},
            "agent_loop": {
                "phases_completed": ["probe", "analyze", "verify"],
                "final_status": "success",
                "context_data": {
                    "policy_route": {"expert_id": "latency_guard"},
                    "canary_verdict": {"status": "accepted"},
                    "rollback": None,
                },
            },
        }
    }

    trace = module.extract_agent_trace(result)

    assert trace["decision"]["source"] == "deepseek-v4"
    assert trace["policy_route"]["expert_id"] == "latency_guard"
    assert trace["canary_verdict"]["status"] == "accepted"
    assert trace["rollback"] is None


def test_agent_trace_summarizes_rollback_without_verbose_entries():
    module = load_demo_module()
    result = {
        "data": {
            "agent_loop": {
                "context_data": {
                    "rollback": {
                        "restored": 4,
                        "groups_removed": 2,
                        "skipped": 0,
                        "entries": [{"path": "/verbose"}],
                        "scx_entries": [
                            {"status": "removed"},
                            {"status": "skipped"},
                        ],
                    }
                }
            }
        }
    }

    trace = module.extract_agent_trace(result)

    assert trace["rollback"] == {
        "restored": 4,
        "groups_removed": 2,
        "skipped": 0,
        "scx_entries_removed": 1,
    }
