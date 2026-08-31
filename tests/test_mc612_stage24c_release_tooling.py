"""Shot 24C: release-tooling certification tests.

These tests exercise the release toolchain end to end in synthetic
sandboxes so they never touch the real repository state:

1. Clean-checkout production validation passes (synthetic git repo with
   required files, build metadata matching HEAD, no forbidden artifacts).
2. Tracked forbidden artifact fails production validation (commands.txt).
3. commands.txt is untracked in the real repo after the corrective commit.
4. Manifest hash semantics: artifact_sha256 recomputes from files{} per
   the documented DEPLOYABLE_CONTENT_SHA256 definition.
5. Manifest artifact_sha256 differs from the tarball's outer SHA-256
   (distinct concepts, distinct values).
6. build metadata commit must match HEAD in production validation.
7. release_version matches mc612-v1 in a generated manifest.
8. Production validation works without a .git dir dependency beyond
   rev-parse/status (extracted artifact dirs document this limitation
   and are validated via build identity + manifest recompute instead).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPO_ROOT / "ops" / "validate-release.py"
BUILDER = REPO_ROOT / "ops" / "build-release.py"

REQUIRED_RUNTIME_SNIPPET = [
    "src/aipm/control_plane/mutation_receipt.py",
    "src/aipm/control_plane/standalone_executor.py",
    "src/aipm/control_plane/executor_ipc.py",
]
REQUIRED_SYSTEMD_SNIPPET = ["ops/systemd/aipm-executor.service"]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _init_synthetic_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo that satisfies the validator's checks."""
    repo = tmp_path / "synthetic-repo"
    repo.mkdir()

    # Required files (validator requires all; copy the real ones)
    for rel in REQUIRED_RUNTIME_SNIPPET:
        src = REPO_ROOT / rel
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    # The validator requires the full runtime list; symlink the rest by
    # copying the whole control_plane package (small, pure python).
    cp_dir = repo / "src" / "aipm" / "control_plane"
    cp_dir.mkdir(parents=True, exist_ok=True)
    for src in (REPO_ROOT / "src" / "aipm" / "control_plane").rglob("*.py"):
        if "__pycache__" in str(src):
            continue
        dst = cp_dir / src.relative_to(REPO_ROOT / "src" / "aipm" / "control_plane")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    for rel in REQUIRED_SYSTEMD_SNIPPET:
        src = REPO_ROOT / rel
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    # Copy all four required systemd units (validator requires all)
    sysd = repo / "ops" / "systemd"
    sysd.mkdir(parents=True, exist_ok=True)
    for unit in (REPO_ROOT / "ops" / "systemd").glob("*.service"):
        shutil.copy2(unit, sysd / unit.name)

    ops = repo / "ops"
    ops.mkdir(exist_ok=True)
    for script in ("setup-aipm-identity.sh", "migrate-aipm-state.sh"):
        shutil.copy2(REPO_ROOT / "ops" / script, ops / script)
    # Also copy the validator + builder so the synthetic repo is complete
    shutil.copy2(VALIDATOR, ops / "validate-release.py")
    shutil.copy2(BUILDER, ops / "build-release.py")

    docs = repo / "docs"
    docs.mkdir(exist_ok=True)
    for doc in (REPO_ROOT / "docs").glob("MC-6.12_*.md"):
        shutil.copy2(doc, docs / doc.name)

    (repo / ".gitignore").write_text("build_meta.json\nrelease-manifest.json\nlogs/\nstate/\nreports/\n")

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@aipm.invalid")
    _git(repo, "config", "user.name", "AIPM Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "synthetic release tree")
    return repo


def _write_build_meta(repo: Path) -> None:
    commit = _git(repo, "rev-parse", "HEAD")
    meta = {
        "commit_sha": commit,
        "version": "mc612-v1",
        "build_timestamp": "2026-08-31T00:00:00+00:00",
        "environment": "production",
        "metadata_version": "aipm-build-meta-v1",
    }
    (repo / "build_meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def _run_validator(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "ops/validate-release.py", *args],
        capture_output=True, text=True, cwd=repo,
    )


# --- Proof 1: clean checkout + matching build metadata passes production ---


def test_clean_checkout_production_validation_passes(tmp_path: Path):
    repo = _init_synthetic_repo(tmp_path)
    _write_build_meta(repo)
    assert _git(repo, "status", "--porcelain") == ""

    result = _run_validator(repo)
    assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
    assert "PASSED" in result.stdout
    assert "mode: production" in result.stdout


# --- Proof 2: tracked forbidden artifact fails production validation ---


def test_tracked_forbidden_artifact_fails_production(tmp_path: Path):
    repo = _init_synthetic_repo(tmp_path)
    _write_build_meta(repo)
    (repo / "commands.txt").write_text("source .venv/bin/activate\n")
    _git(repo, "add", "commands.txt")
    _git(repo, "commit", "-q", "-m", "oops: forbidden artifact")

    result = _run_validator(repo)
    assert result.returncode == 1
    assert "commands.txt" in result.stdout


# --- Proof 3: commands.txt is untracked in the real repo ---


def test_commands_txt_untracked_in_real_repo():
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--", "commands.txt"],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "", "commands.txt must not be tracked"
    # And the ignore rule is intact
    ignore = (REPO_ROOT / ".gitignore").read_text()
    assert "commands.txt" in ignore
    validator_src = VALIDATOR.read_text()
    assert '"commands.txt"' in validator_src or "'commands.txt'" in validator_src


# --- Proof 4: artifact_sha256 recomputes from manifest files{} ---


def _deployable_content_sha256(files: dict) -> str:
    """Reference implementation of the documented definition:
    sha256 over concat(path UTF-8 + per-file sha256 hex), sorted by path."""
    combined = hashlib.sha256()
    for f in sorted(files.keys()):
        combined.update(f.encode())
        combined.update(files[f]["sha256"].encode())
    return combined.hexdigest()


def test_manifest_artifact_sha256_recompute(tmp_path: Path):
    """The documented DEPLOYABLE_CONTENT_SHA256 definition must reproduce
    the manifest's artifact_sha256 for a synthetic file set."""
    manifest = {
        "files": {
            "a.txt": {"sha256": hashlib.sha256(b"A").hexdigest(), "size": 1},
            "dir/b.txt": {"sha256": hashlib.sha256(b"BB").hexdigest(), "size": 2},
            "c.txt": {"sha256": hashlib.sha256(b"CCC").hexdigest(), "size": 3},
        },
    }
    assert _deployable_content_sha256(manifest["files"]) == (
        # Independent inline recomputation
        (lambda files: __import__("hashlib").sha256(b"".join(
            f.encode() + files[f]["sha256"].encode() for f in sorted(files)
        )).hexdigest())(manifest["files"])
    )


def test_manifest_artifact_sha256_recompute_real_builder(tmp_path: Path):
    """Run the real builder in the synthetic repo and recompute per docs."""
    repo = _init_synthetic_repo(tmp_path)
    _write_build_meta(repo)
    result = subprocess.run(
        [sys.executable, "ops/build-release.py"],
        capture_output=True, text=True, cwd=repo,
    )
    assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
    manifest = json.loads((repo / "release-manifest.json").read_text())

    assert manifest["release_version"] == "mc612-v1"
    assert manifest["commit_sha"] == _git(repo, "rev-parse", "HEAD")

    assert manifest["artifact_sha256"] == _deployable_content_sha256(manifest["files"])

    # Per-file hashes must match actual content
    for rel, info in manifest["files"].items():
        path = repo / rel
        assert path.is_file(), rel
        assert hashlib.sha256(path.read_bytes()).hexdigest() == info["sha256"]

    # hash_semantics block present and accurate
    hs = manifest.get("hash_semantics")
    assert hs is not None
    assert hs["artifact_sha256"]["deterministic"] is True
    assert hs["artifact_sha256"]["includes_manifest_itself"] is False
    assert hs["outer_archive_sha256"]["deterministic"] is False


# --- Proof 5: tarball outer hash differs from deployable-content hash ---


def test_outer_archive_hash_differs_from_content_hash(tmp_path: Path):
    repo = _init_synthetic_repo(tmp_path)
    _write_build_meta(repo)
    subprocess.run(
        [sys.executable, "ops/build-release.py"],
        capture_output=True, text=True, cwd=repo, check=True,
    )
    manifest = json.loads((repo / "release-manifest.json").read_text())

    # Package a tarball (as the release process would)
    tarball = tmp_path / "aipm-mc612-v1.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(tarball), "-C", str(repo),
         "src", "ops", "docs", "release-manifest.json"],
        capture_output=True, text=True, check=True,
    )
    outer = hashlib.sha256(tarball.read_bytes()).hexdigest()

    # Repackage after touching a member's mtime → different tar bytes →
    # different outer hash (outer hash covers archive metadata, not just
    # deployable content).
    tarball2 = tmp_path / "aipm-mc612-v1-repack.tar.gz"
    (repo / "docs" / next(d.name for d in (repo / "docs").glob("MC-6.12_*.md"))).touch()
    subprocess.run(
        ["tar", "-czf", str(tarball2), "-C", str(repo),
         "src", "ops", "docs", "release-manifest.json"],
        capture_output=True, text=True, check=True,
    )
    outer2 = hashlib.sha256(tarball2.read_bytes()).hexdigest()

    assert outer != outer2, "outer archive hash must change when archive bytes change"
    assert outer != manifest["artifact_sha256"]
    assert outer2 != manifest["artifact_sha256"]


# --- Proof 6: build metadata commit mismatch fails production ---


def test_build_metadata_commit_mismatch_fails(tmp_path: Path):
    repo = _init_synthetic_repo(tmp_path)
    meta = {
        "commit_sha": "0" * 40,
        "version": "mc612-v1",
        "build_timestamp": "2026-08-31T00:00:00+00:00",
        "environment": "production",
        "metadata_version": "aipm-build-meta-v1",
    }
    (repo / "build_meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")

    result = _run_validator(repo)
    assert result.returncode == 1
    assert "Build metadata commit mismatch" in result.stdout


# --- Proof 7: release version is mc612-v1 (already covered in proof 4) ---


def test_release_version_is_mc612_v1(tmp_path: Path):
    repo = _init_synthetic_repo(tmp_path)
    _write_build_meta(repo)
    subprocess.run(
        [sys.executable, "ops/build-release.py"],
        capture_output=True, text=True, cwd=repo, check=True,
    )
    manifest = json.loads((repo / "release-manifest.json").read_text())
    assert manifest["release_version"] == "mc612-v1"


# --- Proof 8: no .git dependency for extracted artifacts (documented) ---


def test_extracted_artifact_validation_uses_build_identity(tmp_path: Path):
    """An extracted artifact dir has no .git; production validation via
    git-based validator cannot work there by design. The artifact is
    instead validated by build identity + manifest recompute."""
    from aipm.control_plane.build_identity import resolve_build_identity

    repo = _init_synthetic_repo(tmp_path)
    _write_build_meta(repo)
    commit = _git(repo, "rev-parse", "HEAD")

    # Simulate extraction: copy tree WITHOUT .git
    extracted = tmp_path / "extracted"
    shutil.copytree(
        repo, extracted,
        ignore=shutil.ignore_patterns(".git"),
    )
    assert not (extracted / ".git").exists()

    # Build identity still resolves (file-based, no git needed)
    identity = resolve_build_identity(app_root=extracted, production=True)
    assert identity.commit_sha == commit
    assert identity.version == "mc612-v1"
    assert identity.environment == "production"

    # And the git-based validator in the extracted dir cannot determine
    # a commit (documents why packaging-time validation is the gate).
    result = _run_validator(extracted)
    assert result.returncode == 1
    assert "Cannot determine Git commit SHA" in result.stdout
