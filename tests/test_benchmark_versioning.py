import json
import importlib.util
import subprocess
from pathlib import Path

import pytest

from schedx.benchmark.versioning import source_digest, version_identity


@pytest.fixture
def revision(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    for name in ("schedx", "scx", "ebpf"):
        (root / name).mkdir()
    (root / "schedx/main.py").write_text("revision = 1\n")
    (root / "scx/policy.c").write_text("int policy = 1;\n")
    (root / "ebpf/policy.c").write_text("int policy = 2;\n")
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "initial"], check=True)
    binary = tmp_path / "scx_agent"
    binary.write_bytes(b"test binary; not a real kernel scheduler")
    return root, binary


def test_identity_records_actual_commit_source_and_binary(revision):
    root, binary = revision
    identity = version_identity(root, root, "HEAD", binary)
    actual_sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert identity["commit"] == actual_sha
    assert identity["source_sha256"] == source_digest(root)
    assert identity["source_verified_against_git"]
    assert not identity["build_manifest_matches"]


@pytest.mark.parametrize("change", ["modified", "missing", "extra"])
def test_modified_sources_cannot_claim_a_git_revision(revision, change):
    root, binary = revision
    if change == "modified":
        (root / "schedx/main.py").write_text("revision = 999\n")
    elif change == "missing":
        (root / "scx/policy.c").unlink()
    else:
        (root / "schedx/untracked.py").write_text("hidden = True\n")
    with pytest.raises(ValueError, match="sources do not match"):
        version_identity(root, root, "HEAD", binary)


def test_changed_binary_cannot_reuse_build_manifest(revision, tmp_path):
    root, binary = revision
    manifest = tmp_path / "build.json"
    identity = version_identity(root, root, "HEAD", binary)
    manifest.write_text(json.dumps(identity))
    assert version_identity(root, root, "HEAD", binary, manifest)["build_manifest_matches"]
    binary.write_bytes(b"different binary")
    with pytest.raises(ValueError, match="binary_sha256"):
        version_identity(root, root, "HEAD", binary, manifest)


def test_build_outputs_do_not_change_source_identity(revision):
    root, binary = revision
    (root / "scx/build").mkdir()
    (root / "scx/build/generated.h").write_text("temporary header")
    assert version_identity(root, root, "HEAD", binary)["source_file_count"] == 3


def test_legacy_runner_refuses_preliminary_redis_interface(tmp_path):
    spec = importlib.util.spec_from_file_location("optimization_comparison", Path(__file__).resolve().parents[1] / "scripts/run_optimization_benchmarks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "schedx/benchmark"
    path.mkdir(parents=True)
    (path / "runner.py").write_text("class BenchmarkRunner:\n    SUPPORTED = {'nginx-ablation', 'batch-throughput', 'redis-latency'}\n")
    with pytest.raises(ValueError, match="common external measurement adapter"):
        module.require_structured_interface(tmp_path)
