from schedx.agent.context import AgentContext
from schedx.main import build_parser, dry_run_from_args


def test_optimize_default_is_not_dry_run():
    parser = build_parser()
    args = parser.parse_args(["optimize", "--target", "stress-ng", "--mode", "isolate_background"])
    assert dry_run_from_args(args) is False


def test_optimize_explicit_dry_run_is_true():
    parser = build_parser()
    args = parser.parse_args(
        ["optimize", "--target", "stress-ng", "--mode", "isolate_background", "--dry-run"]
    )
    assert dry_run_from_args(args) is True


def test_agent_context_default_is_not_dry_run():
    assert AgentContext().dry_run is False

