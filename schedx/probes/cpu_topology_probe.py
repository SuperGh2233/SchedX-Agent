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
        performance, efficiency = self._split_cores(online)

        return {
            "total_cpus": total,
            "online_cpus": online,
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

    def _split_cores(self, cpus: list[int]) -> tuple[list[int], list[int]]:
        """Split cores into performance and efficiency halves.

        On systems without heterogeneous cores (no big.LITTLE), this simply
        splits by index: first half = performance, second half = efficiency.
        """
        if len(cpus) <= 1:
            return cpus, []
        mid = len(cpus) // 2
        return cpus[:mid], cpus[mid:]

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
            return "0"
        if len(cpus) == 1:
            return str(cpus[0])
        return ",".join(str(c) for c in cpus)
