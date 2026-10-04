"""Coordinate native scheduler ownership with live hard-quota tool calls."""

from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # No native scheduler on non-POSIX hosts.
    fcntl = None


class CpuBackendBusy(RuntimeError):
    pass


def backend_lock_path(sys_root: Path = Path("/sys/kernel/sched_ext")) -> Path:
    if sys_root != Path("/sys/kernel/sched_ext"):
        return sys_root.parent / "schedx-cpu-backend.lock"
    return Path("/run/schedx/cpu-backend.lock")


class CpuBackendLease:
    def __init__(self, path: Path, *, native: bool):
        self.path = path
        self.native = native
        self.fd: int | None = None

    def acquire(self) -> CpuBackendLease:
        if self.fd is not None:
            return self
        if fcntl is None:
            raise RuntimeError("CPU backend ownership requires POSIX file locks")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if self.native else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise CpuBackendBusy("native scheduling and hard CPU quotas cannot run concurrently") from exc
        except OSError:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def release(self) -> None:
        if self.fd is not None:
            # close, rather than LOCK_UN, preserves an inherited child's lease
            # if its caller exits before the workload/scheduler is reaped.
            os.close(self.fd)
            self.fd = None
