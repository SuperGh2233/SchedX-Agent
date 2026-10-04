import pytest

from schedx.agent.context import AgentContext
from schedx.main import build_context, build_parser
from schedx.probes.procfs_probe import ProcessSample, ProcfsProbe
from schedx.skills.probe_skill import ProbeSkill


def test_scoped_probe_reads_only_owned_pids_before_top_limit(monkeypatch, tmp_path):
    probe = ProcfsProbe(tmp_path / "no-host-proc")
    read = []

    def process(pid):
        read.append(pid)
        return ProcessSample(pid, "nginx", "S", 1, "nginx", 1, 1, 4096)

    monkeypatch.setattr(probe, "_read_process", process)
    result = probe.snapshot(interval=0.01, top=1, pids=[23, 21, 23])
    assert read == [21, 23, 21, 23]
    assert len(result["processes"]) == 1
    assert result["scope_pids"] == [21, 23]
    assert result["process_scope"] == "explicit_pids"


def test_empty_scope_does_not_fall_back_to_host_processes(monkeypatch):
    probe = ProcfsProbe()
    monkeypatch.setattr(probe, "_read_process", lambda pid: pytest.fail("must not read a process"))
    assert probe.list_processes([]) == []


@pytest.mark.parametrize("scope", [[True], [-1], ["123"], "123"])
def test_invalid_scope_fails_before_planning(scope):
    context = AgentContext(data={"scope_pids": scope})
    result = ProbeSkill().run(context)
    assert not result.ok
    assert "snapshot" not in context.data


def test_cli_scope_reaches_real_probe_configuration():
    args = build_parser().parse_args(["optimize", "--scope-pid", "21", "--scope-pid", "23"])
    context = build_context(args)
    assert context.data["scope_pids"] == [21, 23]
