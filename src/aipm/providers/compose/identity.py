"""Canonical Docker Compose project identity and provenance resolution."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from aipm.models.project import Project

_COMPOSE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_MAX_NAME_LENGTH = 128
_MAX_COMPOSE_FILES_INSPECTED = 4
_MAX_FILE_BYTES_INSPECTED = 16384


def sanitize_compose_project_name(value: Any) -> str | None:
    """Validate and normalize a candidate Compose project name.

    Returns the normalized lowercase name if it satisfies:
    - Non-empty string of length 1 to 128 characters
    - Starts with an alphanumeric character
    - Contains only lowercase alphanumeric characters, dots, underscores, and hyphens
    - Contains no path characters, wildcards, or shell expressions

    Returns None if the candidate is invalid.
    """
    if not value or not isinstance(value, str):
        return None
    val = value.strip().lower()
    if 1 <= len(val) <= _MAX_NAME_LENGTH and _COMPOSE_NAME_RE.fullmatch(val):
        return val
    return None


def resolve_compose_project_name(project: Project) -> str | None:
    """Resolve the authoritative Docker Compose project name for a project.

    Inspects up to the first 4 declared compose files for a top-level `name:` key.
    If found and valid, returns the declared project name.
    Otherwise, falls back safely to the project's sanitized directory name.
    Returns None if the project is not Compose-capable or lacks a valid name.
    """
    compose_files = list(getattr(project, "compose_files", []) or [])[:_MAX_COMPOSE_FILES_INSPECTED]
    has_compose = bool(getattr(getattr(project, "capabilities", None), "has_compose", False))

    if not compose_files and not has_compose:
        return None

    for filename in compose_files:
        try:
            path = Path(filename)
            if not path.is_file():
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle.read(_MAX_FILE_BYTES_INSPECTED).splitlines():
                    line = raw_line.strip()
                    # Skip empty lines, comments, and indented lines (non-top-level keys)
                    if not line or line.startswith("#") or raw_line[:1].isspace() or not line.startswith("name:"):
                        continue
                    after_colon = line.split(":", 1)[1]
                    # Strip inline comments (e.g. "name: foo # comment")
                    raw_name = after_colon.split("#", 1)[0].strip().strip("'\"").strip()
                    validated = sanitize_compose_project_name(raw_name)
                    if validated is not None:
                        return validated
        except (OSError, UnicodeError):
            continue

    # Fallback to sanitized project directory name
    return sanitize_compose_project_name(getattr(project, "name", None))


def verify_container_provenance(container_labels: dict[str, Any] | None, project: Project) -> bool:
    """Verify that a container originated from the given project's directory.

    Requires matching provenance evidence from Docker Compose labels:
    1. If `com.docker.compose.project.working_dir` is present:
       The resolved path must be equal to or a subpath of `project.path`.
    2. If `com.docker.compose.project.config_files` is present:
       At least one resolved config file path must be inside `project.path` or match
       an explicitly declared compose file in `project.compose_files`.
    3. If neither provenance label is present:
       Fail-closed: returns False to prevent container hijacking or unprovenanced adoption.
    """
    labels = container_labels or {}
    if not getattr(project, "path", None) or not str(project.path).strip():
        return False

    try:
        project_root = Path(project.path).resolve()
    except (OSError, ValueError):
        return False

    raw_workdir = labels.get("com.docker.compose.project.working_dir")
    if raw_workdir and isinstance(raw_workdir, str) and raw_workdir.strip():
        try:
            cand_workdir = Path(raw_workdir.strip()).resolve()
            if cand_workdir.is_relative_to(project_root):
                return True
            return False
        except (OSError, ValueError):
            return False

    raw_configs = labels.get("com.docker.compose.project.config_files")
    if raw_configs and isinstance(raw_configs, str) and raw_configs.strip():
        known_configs = set()
        for p in getattr(project, "compose_files", []) or []:
            if p and str(p).strip():
                try:
                    known_configs.add(Path(str(p).strip()).resolve())
                except (OSError, ValueError):
                    pass

        for part in raw_configs.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                part_path = Path(part).resolve()
                if part_path in known_configs or part_path.is_relative_to(project_root):
                    return True
                rel_path = (project_root / part).resolve()
                if rel_path in known_configs or rel_path.is_relative_to(project_root):
                    return True
            except (OSError, ValueError):
                continue
        return False

    # Fail-closed: both provenance labels are absent
    return False


__all__ = [
    "resolve_compose_project_name",
    "sanitize_compose_project_name",
    "verify_container_provenance",
]
