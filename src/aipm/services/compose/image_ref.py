"""Deterministic, validated container image reference parser.

Conforms to the OCI distribution and Docker image reference grammar
without shell execution, subprocesses, or naive splitting.
"""

from __future__ import annotations

import re

from aipm.models.compose_intelligence import ImageReference

_VALID_REF_CHARS = re.compile(r"^[a-zA-Z0-9_.:/@-]+$")
_DIGEST_RE = re.compile(r"^[a-zA-Z0-9_+.-]+:[a-fA-F0-9]{32,128}$")
_TAG_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")
_MAX_REF_LENGTH = 512


def parse_image_reference(raw: str, *, is_local_build: bool = False) -> ImageReference:
    """Parse and normalize an image reference string into an ImageReference model.

    Handles:
    - Official Docker Hub images: 'redis' -> docker.io/library/redis:latest
    - Tagged images: 'redis:7.2' -> docker.io/library/redis:7.2
    - Organization images: 'dpage/pgadmin4:latest' -> docker.io/dpage/pgadmin4:latest
    - Third-party registries: 'ghcr.io/foo/bar:1.2.3' -> ghcr.io/foo/bar:1.2.3
    - Digest pinned images: 'ghcr.io/foo/bar@sha256:...' -> pinned by digest
    - Port-bearing registries: 'registry.example.com:5000/app:1.0' -> port kept in registry

    Raises ValueError on invalid characters, path traversal, or malformed syntax.
    """
    if not raw or not isinstance(raw, str):
        raise ValueError("Image reference must be a non-empty string")

    clean = raw.strip()
    if len(clean) > _MAX_REF_LENGTH:
        raise ValueError(f"Image reference exceeds maximum length ({_MAX_REF_LENGTH})")

    if "$" in clean:
        raise ValueError(f"Image reference contains unresolved interpolation expression: {clean!r}")

    if not _VALID_REF_CHARS.fullmatch(clean):
        raise ValueError(f"Image reference contains invalid characters: {clean!r}")

    if ".." in clean or "//" in clean or clean.startswith("/") or clean.endswith("/"):
        raise ValueError(f"Image reference contains invalid path structure: {clean!r}")

    digest: str | None = None
    tag: str | None = None
    rem = clean

    # 1. Digest split on '@'
    if "@" in rem:
        rem, digest = rem.split("@", 1)
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError(f"Invalid digest syntax: {digest!r}")

    # 2. Extract tag from remainder if ':' present after the last '/'
    last_slash = rem.rfind("/")
    last_colon = rem.rfind(":")
    if last_colon > last_slash:
        tag = rem[last_colon + 1:]
        rem = rem[:last_colon]
        if not _TAG_RE.fullmatch(tag):
            raise ValueError(f"Invalid tag syntax: {tag!r}")

    # 3. Determine registry and repository
    parts = rem.split("/")
    if len(parts) == 1:
        registry = "docker.io"
        repository = f"library/{parts[0]}"
    elif "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        registry = parts[0]
        repository = "/".join(parts[1:])
    else:
        registry = "docker.io"
        repository = "/".join(parts)

    if not repository:
        raise ValueError("Image reference repository cannot be empty")

    if not tag and not digest:
        tag = "latest"

    is_pinned = digest is not None

    return ImageReference(
        raw=clean,
        registry=registry,
        repository=repository,
        tag=tag,
        digest=digest,
        is_pinned_by_digest=is_pinned,
        is_local_build=is_local_build,
    )
