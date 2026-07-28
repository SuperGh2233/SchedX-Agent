import pytest

from schedx.agent.context import AgentContext
from schedx.main import build_parser, configure_canary, dry_run_from_args


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


def test_optimize_canary_arguments_are_explicitly_configured():
    args = build_parser().parse_args(
        [
            "optimize",
            "--canary-url",
            "http://127.0.0.1/",
            "--canary-duration",
            "3",
            "--canary-min-background-retention",
            "0.3",
            "--canary-min-p99-improvement",
            "25",
        ]
    )
    context = AgentContext()

    configure_canary(context, args)

    assert context.data["canary_config"]["duration"] == 3
    assert context.data["canary_min_background_retention"] == 0.3
    assert context.data["canary_min_p99_improvement"] == 25.0


def test_optimize_rejects_invalid_canary_retention_before_execution():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["optimize", "--canary-min-background-retention", "1.01"]
        )


def test_optimize_rejects_negative_p99_improvement_before_execution():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["optimize", "--canary-min-p99-improvement", "-1"]
        )
