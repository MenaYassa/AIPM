"""Build identity for the AIPM control plane.

Provides deterministic build metadata that proves:
    source commit == release artifact == VPS deployed code == running application

The build metadata is generated at packaging time by writing a
``build_meta.json`` file into the deployment root. In a development
checkout (no build_meta.json), the application reports ``development``
explicitly — production startup MUST fail closed if this file is absent.

No .git dependency. No fake/default commit hash. No "Unknown".
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUILD_METADATA_VERSION = "aipm-build-meta-v1"
BUILD_METADATA_FILENAME = "build_meta.json"
#: Paths relative to the application root where build_meta.json may exist
_SEARCH_PATHS = (
    ".",
    "/opt/aipm/current",
    "/var/lib/aipm/build",
)


class BuildIdentityError(ValueError):
    """Raised when build identity cannot be resolved in a production context."""


@dataclass(frozen=True, slots=True)
class BuildIdentity:
    """Immutable build identity for one deployment."""

    commit_sha: str
    version: str
    build_timestamp: str
    environment: str  # "development" | "production"
    metadata_version: str = BUILD_METADATA_VERSION

    def safe_dict(self) -> dict[str, Any]:
        return {
            "commit_sha": self.commit_sha,
            "version": self.version,
            "build_timestamp": self.build_timestamp,
            "environment": self.environment,
            "metadata_version": self.metadata_version,
        }


def resolve_build_identity(
    *,
    app_root: str | Path | None = None,
    production: bool = False,
) -> BuildIdentity:
    """Resolve build identity from build_meta.json or development fallback.

    In production mode (``production=True``), a missing or malformed
    build_meta.json raises :class:`BuildIdentityError` — the application
    fails closed. In development mode, a safe "development" identity is
    returned with no commit hash (explicitly marked, never "Unknown").
    """
    if app_root is not None:
        search = [Path(app_root)]
    else:
        search = [Path(p) for p in _SEARCH_PATHS]
        # Also try relative to this module
        module_root = Path(__file__).resolve().parents[3]
        if module_root not in search:
            search.insert(0, module_root)

    for root in search:
        meta_file = root / BUILD_METADATA_FILENAME
        if meta_file.is_file():
            try:
                with open(meta_file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                commit = data.get("commit_sha", "")
                version = data.get("version", "")
                timestamp = data.get("build_timestamp", "")
                if not commit or not isinstance(commit, str) or len(commit) < 7:
                    raise BuildIdentityError(f"Invalid commit_sha in {meta_file}")
                if not version or not isinstance(version, str):
                    raise BuildIdentityError(f"Invalid version in {meta_file}")
                return BuildIdentity(
                    commit_sha=commit,
                    version=version,
                    build_timestamp=timestamp,
                    environment=data.get("environment", "production"),
                )
            except (json.JSONDecodeError, KeyError) as exc:
                if production:
                    raise BuildIdentityError(f"Malformed build metadata in {meta_file}: {exc}")
                continue

    # No metadata found
    if production:
        raise BuildIdentityError(
            "Production startup failed: build_meta.json not found. "
            "This deployment was not produced by a valid release process."
        )
    return BuildIdentity(
        commit_sha="",
        version="development",
        build_timestamp="",
        environment="development",
    )


def generate_build_metadata(
    *,
    commit_sha: str,
    version: str,
    build_timestamp: str | None = None,
) -> dict[str, str]:
    """Generate build metadata for a release. Called by the release process."""
    from datetime import datetime, timezone

    if not commit_sha or not isinstance(commit_sha, str) or len(commit_sha) < 7:
        raise BuildIdentityError("Invalid commit_sha for build metadata")
    if not version or not isinstance(version, str):
        raise BuildIdentityError("Invalid version for build metadata")
    ts = build_timestamp or datetime.now(timezone.utc).isoformat()
    return {
        "commit_sha": commit_sha,
        "version": version,
        "build_timestamp": ts,
        "environment": "production",
        "metadata_version": BUILD_METADATA_VERSION,
    }


def write_build_metadata(
    output_dir: str | Path,
    *,
    commit_sha: str,
    version: str,
    build_timestamp: str | None = None,
) -> Path:
    """Write build_meta.json for a release. Called by the release process."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    meta = generate_build_metadata(commit_sha=commit_sha, version=version, build_timestamp=build_timestamp)
    meta_file = output / BUILD_METADATA_FILENAME
    meta_file.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return meta_file
