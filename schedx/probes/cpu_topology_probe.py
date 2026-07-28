from __future__ import annotations

import os
from pathlib import Path


class CpuTopologyProbe:
    """Probe CPU topology for core pinning and domain isolation."""

    def __init__(self, sys_root: Path = Path("/sys/devices/system/cpu")) -> None:
        self.sys_root = sys_root

    def get_topology(self) -> dict:
        total = os.cpu_count() or 1
        online = self._online_cpus()
        performance, efficiency, detection = self._classify_cores(online)

        return {
            "total_cpus": total,
            "online_cpus": online,
            "heterogeneous": bool(efficiency),
            "detection": detection,
            "performance_cores": performance,
            "efficiency_cores": efficiency,
            "performance_mask": self._cpus_to_mask(performance),
            "efficiency_mask": self._cpus_to_mask(efficiency),
        }

    def _online_cpus(self) -> list[int]:
        online_path = self.sys_root / "online"
        if online_path.exists():
            return self._parse_cpu_range(online_path.read_text(encoding="utf-8").strip())
        return list(range(os.cpu_count() or 1))

    def _classify_cores(self, cpus: list[int]) -> tuple[list[int], list[int], str]:
        """Detect heterogeneous cores from sysfs without inventing a split."""
        for relative, source in (
            ("topology/core_type", "core_type"),
            ("cpu_capacity", "cpu_capacity"),
            ("cpufreq/cpuinfo_max_freq", "max_frequency"),
        ):
            values = self._per_cpu_values(cpus, relative)
            if len(values) != len(cpus) or len(set(values.values())) < 2:
                continue
            lowest = min(values.values())
            highest = max(values.values())
            if lowest <= 0 or highest / lowest < 1.10:
                continue
            performance = sorted(cpu for cpu, value in values.items() if value == highest)
            efficiency = sorted(cpu for cpu, value in values.items() if value < highest)
            if performance and efficiency:
                return performance, efficiency, source
        return list(cpus), [], "homogeneous_or_unknown"

    def _per_cpu_values(self, cpus: list[int], relative: str) -> dict[int, int]:
        values: dict[int, int] = {}
        for cpu in cpus:
            path = self.sys_root / f"cpu{cpu}" / relative
            try:
                values[cpu] = int(path.read_text(encoding="utf-8").strip())
            except (FileNotFoundError, OSError, ValueError):
                return {}
        return values

    def get_performance_mask(self) -> str:
        """Return cpuset mask for performance cores."""
        topo = self.get_topology()
        return topo["performance_mask"]

    def get_efficiency_mask(self) -> str:
        """Return cpuset mask for efficiency cores."""
        topo = self.get_topology()
        return topo["efficiency_mask"]

    def get_all_mask(self) -> str:
        """Return cpuset mask for all online CPUs."""
        topo = self.get_topology()
        return self._cpus_to_mask(topo["online_cpus"])

    @staticmethod
    def _parse_cpu_range(text: str) -> list[int]:
        cpus: list[int] = []
        for part in text.split(","):
            part = part.strip()
            if "-" in part:
                lo, hi = part.split("-", 1)
                cpus.extend(range(int(lo), int(hi) + 1))
            elif part.isdigit():
                cpus.append(int(part))
        return sorted(cpus)

    @staticmethod
    def _cpus_to_mask(cpus: list[int]) -> str:
        if not cpus:
            return ""
        if len(cpus) == 1:
            return str(cpus[0])
        return ",".join(str(c) for c in cpus)
