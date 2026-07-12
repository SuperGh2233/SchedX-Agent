from __future__ import annotations

from pathlib import Path


def parse_pressure_file(path: Path) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if not parts:
            continue
        values: dict[str, float] = {}
        for item in parts[1:]:
            if "=" not in item:
                continue
            key, raw = item.split("=", 1)
            try:
                values[key] = float(raw)
            except ValueError:
                continue
        result[parts[0]] = values
    return result


class PressureProbe:
    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root

    def snapshot(self) -> dict[str, dict[str, dict[str, float]]]:
        pressure_root = self.proc_root / "pressure"
        return {
            "cpu": parse_pressure_file(pressure_root / "cpu"),
            "memory": parse_pressure_file(pressure_root / "memory"),
            "io": parse_pressure_file(pressure_root / "io"),
        }

