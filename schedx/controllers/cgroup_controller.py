from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class RollbackEntry:
    path: str
    file: str
    previous: str | None


class CgroupController:
    def __init__(
        self,
        root: Path = Path("/sys/fs/cgroup"),
        managed_prefix: str = "schedx",
        rollback_file: Path = Path(".schedx/rollback.json"),
        dry_run: bool = True,
    ) -> None:
        self.root = root
        self.managed_prefix = managed_prefix
        self.rollback_file = rollback_file
        self.dry_run = dry_run

    @property
    def base_path(self) -> Path:
        return self.root / self.managed_prefix

    def is_v2(self) -> bool:
        return (self.root / "cgroup.controllers").exists()

    def validate_writable(self) -> None:
        if self.dry_run:
            return
        if not self.is_v2():
            raise RuntimeError(
                f"cgroup v2 is not available at {self.root}. Mount cgroup2 before using --apply."
            )
        if not os.access(self.root, os.W_OK):
            raise RuntimeError(
                f"{self.root} is not writable. Run as root or delegate a writable cgroup subtree."
            )
        controllers = (self.root / "cgroup.controllers").read_text(
            encoding="utf-8", errors="replace"
        ).split()
        if "cpu" not in controllers:
            raise RuntimeError("cpu controller is not available in cgroup.controllers.")

    def group_path(self, name: str) -> Path:
        safe = name.strip("/").replace("..", "").replace("/", "_")
        if safe == self.managed_prefix:
            return self.base_path
        if safe.startswith(f"{self.managed_prefix}_"):
            safe = safe[len(self.managed_prefix) + 1 :]
        if safe.startswith(f"{self.managed_prefix}-"):
            safe = safe[len(self.managed_prefix) + 1 :]
        return self.base_path / safe

    def create_group(self, name: str) -> Path:
        self.validate_writable()
        path = self.group_path(name)
        if not self.dry_run:
            self._ensure_cpu_enabled(self.root)
            self.base_path.mkdir(mode=0o755, exist_ok=True)
            self._ensure_cpu_enabled(self.base_path)
            path.mkdir(mode=0o755, exist_ok=True)
        return path

    def add_pid(self, group: str, pid: int) -> None:
        path = self.create_group(group)
        if not self.dry_run:
            current = self._current_cgroup_path(pid)
            if current is not None and current != path and self.base_path not in current.parents:
                self._append_rollback(RollbackEntry(str(current), "cgroup.procs", str(pid)))
        self._write(path / "cgroup.procs", str(pid), record=False)

    def set_cpu_weight(self, group: str, weight: int) -> None:
        if not 1 <= weight <= 10000:
            raise ValueError("cpu.weight must be between 1 and 10000")
        path = self.create_group(group)
        self._write(path / "cpu.weight", str(weight))

    def set_cpu_max(self, group: str, quota: str) -> None:
        parts = quota.split()
        if quota != "max" and len(parts) not in (1, 2):
            raise ValueError('cpu.max must be "max" or "quota period"')
        path = self.create_group(group)
        self._write(path / "cpu.max", quota)

    def set_cpuset_cpus(self, group: str, cpus: str) -> None:
        path = self._prepare_cpuset_group(group)
        self._write(path / "cpuset.cpus", cpus)

    def set_cpuset_mems(self, group: str, mems: str) -> None:
        path = self._prepare_cpuset_group(group)
        self._write(path / "cpuset.mems", mems)

    def _prepare_cpuset_group(self, group: str) -> Path:
        self.validate_writable()
        path = self.group_path(group)
        if not self.dry_run:
            self._ensure_cpu_enabled(self.root)
            self._ensure_cpuset_enabled(self.root)
            self.base_path.mkdir(mode=0o755, exist_ok=True)
            self._ensure_cpu_enabled(self.base_path)
            self._ensure_cpuset_enabled(self.base_path)
            path.mkdir(mode=0o755, exist_ok=True)
        return path

    def _ensure_cpuset_enabled(self, path: Path) -> None:
        subtree = path / "cgroup.subtree_control"
        if not subtree.exists():
            return
        current = subtree.read_text(encoding="utf-8", errors="replace").split()
        if "+cpuset" in current or "cpuset" in current:
            return
        try:
            subtree.write_text("+cpuset", encoding="utf-8")
        except OSError:
            return

    def read_cpu_pressure(self, group: str) -> dict[str, str]:
        path = self.group_path(group) / "cpu.pressure"
        if not path.exists():
            return {}
        return {"raw": path.read_text(encoding="utf-8", errors="replace").strip()}

    def rollback(self) -> list[RollbackEntry | dict]:
        entries = self._load_rollback()
        restored: list[RollbackEntry | dict] = []
        for entry in reversed(entries):
            target = Path(entry.path) / entry.file
            if entry.previous is not None and not self.dry_run:
                try:
                    target.write_text(entry.previous, encoding="utf-8")
                except (FileNotFoundError, ProcessLookupError):
                    restored.append(
                        {
                            "path": str(target),
                            "status": "skipped",
                            "reason": "target_process_exited",
                        }
                    )
                    continue
                except OSError as exc:
                    restored.append(
                        {
                            "path": str(target),
                            "status": "skipped",
                            "reason": f"restore_failed: {exc}",
                        }
                    )
                    continue
            restored.append(entry)
        if not self.dry_run:
            restored.extend(self.cleanup_empty_groups())
        if not self.dry_run and self.rollback_file.exists():
            self.rollback_file.unlink()
        return restored

    def cleanup_empty_groups(self) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        base = self.base_path
        if not base.exists():
            return results
        for child in sorted(base.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            results.append(self._try_remove_group(child))
        if base.exists():
            results.append(self._try_remove_base_group(base))
        return results

    def _write(self, path: Path, value: str, record: bool = True) -> None:
        self.validate_writable()
        if record:
            previous = None
            if path.exists():
                try:
                    previous = path.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    previous = None
            self._append_rollback(RollbackEntry(str(path.parent), path.name, previous))
        if not self.dry_run:
            path.write_text(value, encoding="utf-8")

    def _ensure_cpu_enabled(self, path: Path) -> None:
        subtree = path / "cgroup.subtree_control"
        if not subtree.exists():
            return
        current = subtree.read_text(encoding="utf-8", errors="replace").split()
        if "+cpu" in current or "cpu" in current:
            return
        try:
            subtree.write_text("+cpu", encoding="utf-8")
        except OSError:
            # Some delegated or busy cgroups cannot enable controllers here.
            # The later cpu.weight write will produce the actionable error.
            return

    def _try_remove_group(self, path: Path) -> dict[str, str]:
        procs = path / "cgroup.procs"
        if procs.exists():
            raw = procs.read_text(encoding="utf-8", errors="replace").strip()
            if raw:
                return {"path": str(path), "status": "skipped", "reason": "process_still_alive"}
        try:
            path.rmdir()
            return {"path": str(path), "status": "removed"}
        except OSError:
            return {"path": str(path), "status": "skipped", "reason": "not_empty"}

    def _try_remove_base_group(self, path: Path) -> dict[str, str]:
        removable, reason = self._base_group_removable(path)
        if not removable:
            return {"path": str(path), "status": "skipped", "reason": reason}
        try:
            path.rmdir()
            return {"path": str(path), "status": "removed"}
        except OSError:
            return {"path": str(path), "status": "skipped", "reason": "not_empty"}

    def _base_group_removable(self, path: Path) -> tuple[bool, str]:
        procs = path / "cgroup.procs"
        if procs.exists() and procs.read_text(encoding="utf-8", errors="replace").strip():
            return False, "process_still_alive"
        if any(child.is_dir() and child.name.startswith("pid-") for child in path.iterdir()):
            return False, "not_empty"
        return True, ""

    def _load_rollback(self) -> list[RollbackEntry]:
        if not self.rollback_file.exists():
            return []
        raw = json.loads(self.rollback_file.read_text(encoding="utf-8"))
        return [RollbackEntry(**item) for item in raw]

    def _current_cgroup_path(self, pid: int) -> Path | None:
        cgroup_file = Path("/proc") / str(pid) / "cgroup"
        try:
            for line in cgroup_file.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split(":", 2)
                if len(parts) == 3 and parts[0] == "0":
                    relative = parts[2].lstrip("/")
                    return self.root / relative
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            return None
        return None

    def _append_rollback(self, entry: RollbackEntry) -> None:
        entries = self._load_rollback()
        entries.append(entry)
        if not self.dry_run:
            self.rollback_file.parent.mkdir(parents=True, exist_ok=True)
            self.rollback_file.write_text(
                json.dumps([asdict(e) for e in entries], indent=2),
                encoding="utf-8",
            )
