from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from schedx.benchmark.wrk_parser import parse_wrk_output


@dataclass(frozen=True)
class WrkCanaryConfig:
    url: str
    duration: int = 5
    connections: int = 32
    threads: int = 2

    def __post_init__(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("canary URL must use http:// or https://")
        if self.duration < 1:
            raise ValueError("canary duration must be at least one second")
        if self.connections < 1 or self.threads < 1:
            raise ValueError("canary connections and threads must be positive")


class WrkSloProbe:
    """Collect a bounded wrk sample and workload progress evidence."""

    def __init__(
        self,
        proc_root: Path = Path("/proc"),
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.proc_root = proc_root
        self.runner = runner

    def is_available(self) -> bool:
        return shutil.which("wrk") is not None

    def sample(self, config: WrkCanaryConfig, background_pids: list[int]) -> dict:
        if not self.is_available():
            raise RuntimeError("wrk not found; install wrk before enabling SLO canary")

        background_before = self._process_cpu_ticks(background_pids)
        system_before = self._system_cpu_ticks()
        command = [
            "wrk",
            f"-t{config.threads}",
            f"-c{config.connections}",
            f"-d{config.duration}s",
            "--latency",
            config.url,
        ]
        completed = self.runner(
            command,
            capture_output=True,
            text=True,
            timeout=config.duration + 15,
            check=False,
        )
        background_after = self._process_cpu_ticks(background_pids)
        system_after = self._system_cpu_ticks()
        raw_output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            raise RuntimeError(
                f"wrk canary failed with exit code {completed.returncode}: "
                f"{raw_output.strip()}"
            )

        metrics = parse_wrk_output(raw_output)
        if "requests_per_sec" not in metrics:
            raise RuntimeError("wrk canary output did not contain Requests/sec")
        background_ticks = max(0, background_after - background_before)
        system_ticks = max(0, system_after - system_before)
        metrics.update(
            {
                "background_cpu_ticks": background_ticks,
                "system_cpu_ticks": system_ticks,
                "background_cpu_share": (
                    background_ticks / system_ticks if system_ticks else None
                ),
                "background_pids": background_pids,
                "raw_output": raw_output,
            }
        )
        return metrics

    def _process_cpu_ticks(self, pids: list[int]) -> int:
        total = 0
        for pid in pids:
            try:
                text = (self.proc_root / str(pid) / "stat").read_text(encoding="utf-8")
                fields = text[text.rfind(")") + 2 :].split()
                total += int(fields[11]) + int(fields[12])
            except (FileNotFoundError, OSError, IndexError, ValueError):
                continue
        return total

    def _system_cpu_ticks(self) -> int:
        try:
            first = (self.proc_root / "stat").read_text(encoding="utf-8").splitlines()[0]
            return sum(int(value) for value in first.split()[1:])
        except (FileNotFoundError, OSError, IndexError, ValueError):
            return 0


def background_pids(classification: dict) -> list[int]:
    return sorted(
        {
            int(process["pid"])
            for process in classification.get("groups", {}).get("background_noise", [])
            if str(process.get("pid", "")).isdigit()
        }
    )


def sched_ext_rejected(path: Path = Path("/sys/kernel/sched_ext/nr_rejected")) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return 0
