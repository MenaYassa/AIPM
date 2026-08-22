from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from aipm.control_plane.identity import PLAN_IDENTITY_VERSION
from aipm.provenance.release import validate_release


def _release(tmp_path: Path, commit: str = "e9b4e6d520609eb3fb1f87f00036799825e754fa") -> Path:
    root = tmp_path / "release"
    root.mkdir()
    app = root / "app"
    app.mkdir()
    payload = app / "module.py"
    payload.write_text("value = 1\n", encoding="utf-8")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    manifest_body = f"{digest}  app/module.py\n"
    manifest = root / "manifest.sha256"
    manifest.write_text(manifest_body, encoding="utf-8")
    metadata = {
        "release_id": "release-test",
        "source_commit": commit,
        "python_version": "3.12.3",
        "cryptography_version": "50.0.0",
        "manifest_sha256": hashlib.sha256(manifest_body.encode()).hexdigest(),
        "plan_identity_version": PLAN_IDENTITY_VERSION,
    }
    (root / "release.json").write_text(json.dumps(metadata), encoding="utf-8")
    return root


def test_release_manifest_and_metadata_validate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _release(tmp_path)
    monkeypatch.setattr("aipm.provenance.release.version", lambda name: "50.0.0")
    result = validate_release(root, expected_commit="e9b4e6d520609eb3fb1f87f00036799825e754fa")
    assert result["plan_identity_version"] == PLAN_IDENTITY_VERSION


def test_release_rejects_symlinked_metadata_and_manifest(tmp_path: Path) -> None:
    root = _release(tmp_path)
    metadata = root / "release.json"
    metadata_target = root / "metadata-target.json"
    metadata_target.write_bytes(metadata.read_bytes())
    metadata.unlink()
    os.symlink(metadata_target, metadata)
    with pytest.raises(ValueError, match="release metadata"):
        validate_release(root)

    payload_tmp = tmp_path / "payload"
    payload_tmp.mkdir()
    root = _release(payload_tmp)
    payload = root / "app" / "module.py"
    payload_target = root / "app" / "module-target.py"
    payload_target.write_text(payload.read_text(encoding="utf-8"), encoding="utf-8")
    payload.unlink()
    os.symlink(payload_target, payload)
    with pytest.raises(ValueError, match="forbidden release path"):
        validate_release(root)


def test_release_rejects_private_home_runtime(tmp_path: Path) -> None:
    home = tmp_path / "home" / "ubuntu" / "release"
    home.mkdir(parents=True)
    with pytest.raises(ValueError):
        validate_release(home)
