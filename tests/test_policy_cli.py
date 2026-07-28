import json
from pathlib import Path

from schedx.main import build_parser


def test_policies_command_lists_builtin_experts_without_root(
    tmp_path: Path, capsys
):
    parser = build_parser()
    args = parser.parse_args(["policies", "--state-dir", str(tmp_path)])

    returncode = args.func(args)
    payload = json.loads(capsys.readouterr().out)

    assert returncode == 0
    assert payload["repository"].endswith("policy_repository.json")
    assert {item["expert_id"] for item in payload["experts"]} == {
        "latency_guard",
        "throughput_boost",
        "background_isolation",
        "balanced",
    }
    assert not (tmp_path / "policy_repository.json").exists()

