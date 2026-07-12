from __future__ import annotations

from pathlib import Path


class CgroupProbe:
    def __init__(self, root: Path = Path("/sys/fs/cgroup")) -> None:
        self.root = root

    def is_v2(self) -> bool:
        return (self.root / "cgroup.controllers").exists()

    def controllers(self) -> list[str]:
        path = self.root / "cgroup.controllers"
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").split()

    def cpu_pressure(self) -> str:
        path = self.root / "cpu.pressure"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace").strip()

    def cpu_stat(self, group: str = "") -> dict[str, int]:
        path = self.root / group.strip("/") / "cpu.stat" if group else self.root / "cpu.stat"
        if not path.exists():
            return {}
        result: dict[str, int] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                continue
        return result

    def snapshot(self) -> dict:
        return {
            "v2": self.is_v2(),
            "controllers": self.controllers(),
            "cpu_pressure": self.cpu_pressure(),
            "cpu_stat": self.cpu_stat(),
        }
