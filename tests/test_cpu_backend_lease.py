import subprocess
import sys

import pytest

from schedx.cpu_backend import CpuBackendBusy, CpuBackendLease


def test_live_hard_quota_leases_block_native_start_but_allow_other_quotas(tmp_path):
    path = tmp_path / "backend.lock"
    first = CpuBackendLease(path, native=False).acquire()
    second = CpuBackendLease(path, native=False).acquire()
    try:
        with pytest.raises(CpuBackendBusy):
            CpuBackendLease(path, native=True).acquire()
    finally:
        first.release()
        second.release()
    native = CpuBackendLease(path, native=True).acquire()
    try:
        with pytest.raises(CpuBackendBusy):
            CpuBackendLease(path, native=False).acquire()
    finally:
        native.release()


def test_child_keeps_quota_lease_after_parent_closes_its_descriptor(tmp_path):
    path = tmp_path / "backend.lock"
    quota = CpuBackendLease(path, native=False).acquire()
    child = subprocess.Popen([sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"],
                             pass_fds=(quota.fd,), stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        quota.release()
        with pytest.raises(CpuBackendBusy):
            CpuBackendLease(path, native=True).acquire()
    finally:
        quota.release()
        child.terminate()
        child.wait(timeout=5)
        child.stdout.close()
    native = CpuBackendLease(path, native=True).acquire()
    native.release()


def test_quota_lease_survives_child_exec(tmp_path):
    path = tmp_path / "backend.lock"
    quota = CpuBackendLease(path, native=False).acquire()
    command = "import os,sys; os.execv(sys.executable, [sys.executable, '-c', \"import time; print('exec-ready',flush=True); time.sleep(60)\"])"
    child = subprocess.Popen([sys.executable, "-c", command], pass_fds=(quota.fd,), stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "exec-ready"
        quota.release()
        with pytest.raises(CpuBackendBusy):
            CpuBackendLease(path, native=True).acquire()
    finally:
        quota.release()
        child.terminate()
        child.wait(timeout=5)
        child.stdout.close()
