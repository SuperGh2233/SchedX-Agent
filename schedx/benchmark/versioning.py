"""Bind measured sources to Git objects and record actual binary fingerprints."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path


DIRECTORIES = ("schedx", "scx", "ebpf")


def included(path: Path) -> bool:
    return path.suffix in {".py", ".c", ".h"} and not any(
        part in {"build", "output", "__pycache__"} for part in path.parts
    )


def digest_files(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    # Preserve the order used by the historical optimization fingerprints.
    for directory in DIRECTORIES:
        for name in sorted(name for name in files if name.startswith(directory + "/")):
            digest.update(name.encode())
            digest.update(files[name])
    return digest.hexdigest()


def core_files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for directory in DIRECTORIES
        for path in (root / directory).rglob("*")
        if path.is_file() and included(path.relative_to(root))
    }


def source_digest(root: Path) -> str:
    return digest_files(core_files(root))


def version_identity(repository: Path, source: Path, revision: str, binary: Path,
                     build_manifest: Path | None = None) -> dict:
    resolved = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", "--end-of-options", revision + "^{commit}"],
        capture_output=True, check=True, text=True, timeout=10,
    ).stdout.strip()
    archive = subprocess.run(
        ["git", "-C", str(repository), "archive", resolved, *DIRECTORIES],
        capture_output=True, check=True, timeout=30,
    ).stdout
    expected = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for entry in bundle.getmembers():
            if entry.isfile() and included(Path(entry.name)):
                expected[entry.name] = bundle.extractfile(entry).read()
    if not expected:
        raise ValueError("the requested Git object has no production sources")
    actual = core_files(source)
    missing, extra = set(expected) - set(actual), set(actual) - set(expected)
    changed = {name for name in set(actual) & set(expected) if actual[name] != expected[name]}
    if missing or extra or changed:
        raise ValueError(f"sources do not match {resolved}: missing={sorted(missing)}, extra={sorted(extra)}, changed={sorted(changed)}")
    record = {
        "commit": resolved, "source_path": str(source.resolve()),
        "source_sha256": digest_files(actual), "source_verified_against_git": True,
        "source_file_count": len(actual), "binary_path": str(binary.resolve()),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "build_manifest_matches": False,
    }
    if build_manifest is not None:
        manifest = json.loads(build_manifest.read_text())
        for name in ("commit", "source_sha256", "binary_sha256"):
            if manifest.get(name) != record[name]:
                raise ValueError(f"build manifest does not match {name}")
        record["build_manifest_matches"] = True
        record["build_manifest"] = manifest
    return record
