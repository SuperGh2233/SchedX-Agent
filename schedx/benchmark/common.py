from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


def process_ids(*names: str) -> list[int]:
    found: set[int] = set()
    for name in names:
        try:
            completed = subprocess.run(
                ["pgrep", "-x", name],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            continue
        found.update(
            int(value) for value in completed.stdout.split() if value.isdigit()
        )
    return sorted(found)


def cpu_ticks(pids: Sequence[int]) -> int:
    total = 0
    for pid in pids:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            total += int(fields[13]) + int(fields[14])
        except (FileNotFoundError, IndexError, ValueError):
            continue
    return total


def start_stress(cpu_workers: int, timeout_seconds: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            "stress-ng",
            "--cpu",
            str(cpu_workers),
            "--timeout",
            f"{timeout_seconds}s",
            "--metrics-brief",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )


def stop_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
        process.wait(timeout=5)


def cleanup_stress() -> None:
    for name in ("stress-ng-cpu", "stress-ng"):
        try:
            subprocess.run(
                ["pkill", "-x", name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            continue


def run_schedx_json(args: Sequence[str], timeout: int = 120) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "schedx.main", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        data = {}
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "data": data,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def mean(rows: Sequence[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return round(sum(values) / len(values), 4) if values else None


def percent_gain(before: Any, after: Any) -> float | None:
    if before in (None, 0) or after is None:
        return None
    return round((float(after) - float(before)) / float(before) * 100.0, 4)


def percent_drop(before: Any, after: Any) -> float | None:
    if before in (None, 0) or after is None:
        return None
    return round((float(before) - float(after)) / float(before) * 100.0, 4)
