import subprocess

from schedx.agent.context import AgentContext
from schedx.probes.slo_probe import WrkCanaryConfig, WrkSloProbe
from schedx.skills.canary_skill import CanaryBaselineSkill, CanaryCandidateSkill
from schedx.skills.verify_skill import VerifySkill


def wrk_output(rps: float, p99_ms: float) -> str:
    return f"""
  Latency   1.00ms  500.00us  10.00ms
  Req/Sec    10.00k     1.00k
  50%    0.70ms
  75%    1.10ms
  90%    2.00ms
  99%    {p99_ms:.2f}ms
Requests/sec:  {rps:.2f}
Transfer/sec:  10.00MB
"""


class SequenceProbe(WrkSloProbe):
    def __init__(self, outputs, process_ticks, system_ticks):
        self.outputs = list(outputs)
        self.process_ticks = list(process_ticks)
        self.system_ticks = list(system_ticks)
        super().__init__(runner=self._run)

    def is_available(self) -> bool:
        return True

    def _run(self, command, **kwargs):
        return subprocess.CompletedProcess(command, 0, self.outputs.pop(0), "")

    def _process_cpu_ticks(self, pids):
        return self.process_ticks.pop(0)

    def _system_cpu_ticks(self):
        return self.system_ticks.pop(0)


def test_wrk_probe_collects_slo_and_background_progress():
    probe = SequenceProbe([wrk_output(1000, 4)], [100, 140], [1000, 1200])

    result = probe.sample(WrkCanaryConfig("http://127.0.0.1/"), [10, 11])

    assert result["requests_per_sec"] == 1000
    assert result["p99_ms"] == 4
    assert result["background_cpu_ticks"] == 40
    assert result["background_cpu_share"] == 0.2


def test_canary_skills_capture_before_after_and_verify_accepts(monkeypatch):
    probe = SequenceProbe(
        [wrk_output(1000, 5), wrk_output(1100, 4)],
        [100, 200, 200, 260],
        [1000, 1200, 1200, 1400],
    )
    context = AgentContext()
    context.data.update(
        {
            "canary_config": {"url": "http://127.0.0.1/", "duration": 1},
            "classification": {
                "groups": {"background_noise": [{"pid": 42, "comm": "stress-ng"}]}
            },
            "execution_results": [{"status": "ok"}],
            "mode": "latency_first",
            "_slo_probe": probe,
        }
    )
    monkeypatch.setattr(VerifySkill, "_check_system_state", lambda self: {})

    assert CanaryBaselineSkill().run(context).ok
    assert CanaryCandidateSkill().run(context).ok
    verified = VerifySkill().run(context)

    assert verified.ok
    assert context.data["canary"]["background_retention"] == 0.6
    assert context.data["canary_verdict"]["status"] == "accepted"


def test_canary_rejects_background_progress_below_floor(monkeypatch):
    probe = SequenceProbe(
        [wrk_output(1000, 5), wrk_output(1200, 3)],
        [100, 200, 200, 210],
        [1000, 1200, 1200, 1400],
    )
    context = AgentContext()
    context.data.update(
        {
            "canary_config": {"url": "http://127.0.0.1/", "duration": 1},
            "classification": {
                "groups": {"background_noise": [{"pid": 42, "comm": "stress-ng"}]}
            },
            "execution_results": [{"status": "ok"}],
            "mode": "latency_first",
            "_slo_probe": probe,
        }
    )
    monkeypatch.setattr(VerifySkill, "_check_system_state", lambda self: {})

    CanaryBaselineSkill().run(context)
    CanaryCandidateSkill().run(context)
    verified = VerifySkill().run(context)

    assert not verified.ok
    assert context.data["rollback_required"] is True
    assert "background_progress_regression" in context.data["canary_verdict"]["reasons"]
