"""Locked, durable JSON state shared by resource-control processes."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


@contextmanager
def state_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def process_start_time(pid: int, proc_root: Path = Path("/proc")) -> str | None:
    try:
        text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        return text[text.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return None


def restoration_failed(entries: list) -> bool:
    return any(
        isinstance(entry, dict) and (
            entry.get("status") in {"failed", "rollback_failed"}
            or str(entry.get("reason", "")).startswith("restore_failed")
        )
        for entry in entries
    )
