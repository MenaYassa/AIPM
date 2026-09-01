#!/usr/bin/env python3
"""Generate release metadata and manifest for the MC-6.12 control plane.

Creates:
- build_meta.json
    Release identity metadata (commit, version, timestamp, environment)
- release-manifest.json
    Deployable file inventory with SHA-256 checksums

Run from a clean release tree after validation passes.

Usage:
    python ops/build-release.py

The manifest does NOT include:
.venv, __pycache__, caches, logs, state,
reports, models, secrets, tests, or unrelated files.

build_meta.json and release-manifest.json are generated release outputs
and are intentionally excluded from deployable-content hashing.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from aipm.control_plane.build_identity import write_build_metadata


VERSION = "mc612-v1"

RUNTIME_FILES = sorted([
    str(p)
    for p in Path("src/aipm/control_plane").rglob("*.py")
    if "__pycache__" not in str(p)
])

SYSTEMD_FILES = sorted([
    str(p)
    for p in Path("ops/systemd").iterdir()
    if p.is_file() and p.suffix in (".service", ".socket")
    and "__pycache__" not in str(p)
])

OPS_FILES = [
    "ops/setup-aipm-identity.sh",
    "ops/migrate-aipm-state.sh",
    "ops/validate-release.py",
    "ops/build-release.py",
]

DOC_FILES = sorted([
    str(p)
    for p in Path("docs").glob("MC-6.12_*.md")
    if p.is_file()
])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def assert_clean_tree() -> None:
    staged = subprocess.run(
        ["git", "diff", "--cached", "--stat"],
        capture_output=True,
        text=True,
        check=True,
    )

    unstaged = subprocess.run(
        ["git", "diff", "--stat"],
        capture_output=True,
        text=True,
        check=True,
    )

    if staged.stdout.strip() or unstaged.stdout.strip():
        print(
            "ERROR: Git tree has uncommitted changes. "
            "Release must be from a clean commit.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def generate_manifest(commit: str) -> dict:
    all_files = RUNTIME_FILES + SYSTEMD_FILES + OPS_FILES + DOC_FILES

    manifest = {
        "release_version": VERSION,
        "commit_sha": commit,
        "generated_from": "clean git tree",
        "files": {},
    }

    for filename in sorted(set(all_files)):
        path = Path(filename)

        if path.is_file():
            manifest["files"][filename] = {
                "sha256": sha256(path),
                "size": path.stat().st_size,
            }

    combined = hashlib.sha256()

    for filename in sorted(manifest["files"].keys()):
        combined.update(filename.encode())
        combined.update(
            manifest["files"][filename]["sha256"].encode()
        )

    manifest["artifact_sha256"] = combined.hexdigest()

    manifest["hash_semantics"] = {
        "artifact_sha256": {
            "definition": (
                "DEPLOYABLE_CONTENT_SHA256: SHA-256 over the concatenation "
                "of (relative path UTF-8 bytes + per-file sha256 hex) for "
                "every file listed in manifest['files'], iterated in sorted "
                "path order."
            ),
            "deterministic": True,
            "includes_manifest_itself": False,
            "includes_build_meta": False,
            "includes_tar_metadata": False,
            "recompute": (
                "sort keys of manifest['files']; "
                "sha256(path_bytes + file_sha_hex) chained"
            ),
        },
        "outer_archive_sha256": {
            "definition": (
                "OUTER_ARCHIVE_SHA256: plain SHA-256 of the release .tar.gz "
                "file. It covers archive bytes (tar member metadata + gzip "
                "header), NOT deployable content as defined by "
                "artifact_sha256."
            ),
            "deterministic": False,
            "recorded_at": "packaging time, next to the tarball",
        },
    }

    return manifest


def main() -> int:
    assert_clean_tree()

    commit = git_commit()

    # Generate release identity metadata.
    # This was previously a missing release step.
    build_meta = write_build_metadata(
        ".",
        commit_sha=commit,
        version=VERSION,
    )

    print(f"Build metadata written: {build_meta}")

    manifest = generate_manifest(commit)

    output = Path("release-manifest.json")
    output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"Release manifest written: {output}")
    print(f"  commit: {commit}")
    print(f"  files: {len(manifest['files'])}")
    print(f"  artifact_sha256: {manifest['artifact_sha256']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())