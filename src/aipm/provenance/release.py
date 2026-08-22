"""Read-only validation for an immutable MC-6.12B release tree."""
from __future__ import annotations

import hashlib
import json
from importlib.metadata import version
from pathlib import Path

from aipm.control_plane.identity import PLAN_IDENTITY_VERSION

FORBIDDEN_PARTS = {".git", ".env", "__pycache__"}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm"}


def _safe_path(root: Path, child: Path) -> bool:
    resolved = child.resolve()
    return resolved == root.resolve() or root.resolve() in resolved.parents


def validate_release(root: str | Path, *, expected_commit: str | None = None) -> dict[str, str]:
    release_root = Path(root).resolve()
    parts = release_root.parts
    private_pairs = {("home", "ubuntu"), ("home", "mina")}
    if any(tuple(parts[index:index + 2]) in private_pairs for index in range(max(0, len(parts) - 1))):
        raise ValueError("runtime release must be outside private home paths")
    manifest_path = release_root / "manifest.sha256"
    metadata_path = release_root / "release.json"
    if (
        not release_root.is_dir()
        or manifest_path.is_symlink()
        or metadata_path.is_symlink()
        or not manifest_path.is_file()
        or not metadata_path.is_file()
    ):
        raise ValueError("release metadata is incomplete")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    required = {"release_id", "source_commit", "python_version", "cryptography_version", "manifest_sha256", "plan_identity_version"}
    if set(metadata) != required or metadata["plan_identity_version"] != PLAN_IDENTITY_VERSION:
        raise ValueError("release metadata schema mismatch")
    if expected_commit is not None and metadata["source_commit"] != expected_commit:
        raise ValueError("unexpected source commit")
    if metadata["cryptography_version"] != version("cryptography"):
        raise ValueError("dependency version mismatch")
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if manifest_hash != metadata["manifest_sha256"]:
        raise ValueError("manifest metadata mismatch")
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError("invalid manifest line")
        digest, rel = parts
        child = release_root / rel
        if (
            not _safe_path(release_root, child)
            or child.is_symlink()
            or any(part in FORBIDDEN_PARTS for part in child.parts)
            or child.suffix in FORBIDDEN_SUFFIXES
        ):
            raise ValueError("forbidden release path")
        if not child.is_file() or hashlib.sha256(child.read_bytes()).hexdigest() != digest:
            raise ValueError("release file hash mismatch")
    return {"release_id": metadata["release_id"], "source_commit": metadata["source_commit"], "plan_identity_version": metadata["plan_identity_version"]}
