from pathlib import Path

from schedx.probes.cpu_topology_probe import CpuTopologyProbe


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def test_homogeneous_cpu_is_not_split_into_fake_core_classes(tmp_path: Path):
    _write(tmp_path / "online", "0-3\n")

    topology = CpuTopologyProbe(tmp_path).get_topology()

    assert topology["heterogeneous"] is False
    assert topology["performance_cores"] == [0, 1, 2, 3]
    assert topology["efficiency_cores"] == []
    assert topology["efficiency_mask"] == ""
    assert topology["detection"] == "homogeneous_or_unknown"


def test_core_type_detects_real_heterogeneous_topology(tmp_path: Path):
    _write(tmp_path / "online", "0-3\n")
    for cpu, core_type in ((0, 2), (1, 2), (2, 1), (3, 1)):
        _write(tmp_path / f"cpu{cpu}" / "topology" / "core_type", str(core_type))

    topology = CpuTopologyProbe(tmp_path).get_topology()

    assert topology["heterogeneous"] is True
    assert topology["performance_cores"] == [0, 1]
    assert topology["efficiency_cores"] == [2, 3]
    assert topology["detection"] == "core_type"


def test_small_frequency_noise_does_not_create_fake_classes(tmp_path: Path):
    _write(tmp_path / "online", "0-1\n")
    _write(tmp_path / "cpu0/cpufreq/cpuinfo_max_freq", "3000000")
    _write(tmp_path / "cpu1/cpufreq/cpuinfo_max_freq", "2950000")

    topology = CpuTopologyProbe(tmp_path).get_topology()

    assert topology["heterogeneous"] is False
