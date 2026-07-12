from pathlib import Path

from schedx.probes.procfs_probe import ProcfsProbe
from schedx.probes.procfs_probe import ProcessSample


def test_procfs_probe_handles_missing_proc(tmp_path: Path):
    snapshot = ProcfsProbe(tmp_path / "missing").snapshot(interval=0.01)
    assert snapshot["processes"] == []


def test_procfs_probe_filters_kernel_thread():
    proc = ProcessSample(
        pid=42,
        comm="kworker/0:1",
        state="S",
        ppid=2,
        cmdline="",
        utime=0,
        stime=0,
        rss_bytes=0,
    )
    assert ProcfsProbe().is_kernel_thread(proc)


def test_procfs_probe_does_not_filter_user_process():
    proc = ProcessSample(
        pid=43,
        comm="nginx",
        state="S",
        ppid=1,
        cmdline="nginx: worker process",
        utime=0,
        stime=0,
        rss_bytes=4096,
    )
    assert not ProcfsProbe().is_kernel_thread(proc)
