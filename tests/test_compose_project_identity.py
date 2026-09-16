"""Tests for canonical Docker Compose project identity and provenance resolution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aipm.models.project import Project, ProjectCapabilities
from aipm.models.update import UpdateRisk
from aipm.providers.compose.identity import (
    resolve_compose_project_name,
    sanitize_compose_project_name,
    verify_container_provenance,
)
from aipm.providers.compose.provider import ComposeProvider
from aipm.services.project.intelligence import ProjectIntelligenceService
from aipm.services.update.planner import UpdatePlanner


# ---------------------------------------------------------------------------
# Unit tests: sanitize_compose_project_name
# ---------------------------------------------------------------------------


def test_sanitize_compose_project_name_valid() -> None:
    assert sanitize_compose_project_name("localai") == "localai"
    assert sanitize_compose_project_name("my-stack_v2.0") == "my-stack_v2.0"
    assert sanitize_compose_project_name("LOCALAI") == "localai"
    assert sanitize_compose_project_name("  trimmed_name  ") == "trimmed_name"


def test_sanitize_compose_project_name_invalid() -> None:
    assert sanitize_compose_project_name("") is None
    assert sanitize_compose_project_name(None) is None
    assert sanitize_compose_project_name(123) is None
    assert sanitize_compose_project_name("bad name with spaces") is None
    assert sanitize_compose_project_name("../path/traversal") is None
    assert sanitize_compose_project_name("rm -rf /") is None
    assert sanitize_compose_project_name("wildcard*") is None
    assert sanitize_compose_project_name("-starts-with-dash") is None
    assert sanitize_compose_project_name("a" * 129) is None


# ---------------------------------------------------------------------------
# Unit tests: resolve_compose_project_name
# ---------------------------------------------------------------------------


def test_resolve_compose_project_name_from_top_level_name(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: localai\nservices:\n  app:\n    image: redis\n")

    proj = Project(
        name="local-ai-packaged",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )
    assert resolve_compose_project_name(proj) == "localai"


def test_resolve_compose_project_name_quoted(tmp_path: Path) -> None:
    compose_file_dq = tmp_path / "compose-dq.yml"
    compose_file_dq.write_text('name: "double-quoted-stack"\nservices:\n')
    proj_dq = Project(
        name="fallback-name",
        path=str(tmp_path),
        compose_files=[str(compose_file_dq)],
        capabilities=ProjectCapabilities(has_compose=True),
    )
    assert resolve_compose_project_name(proj_dq) == "double-quoted-stack"

    compose_file_sq = tmp_path / "compose-sq.yml"
    compose_file_sq.write_text("name: 'single-quoted-stack'\nservices:\n")
    proj_sq = Project(
        name="fallback-name",
        path=str(tmp_path),
        compose_files=[str(compose_file_sq)],
        capabilities=ProjectCapabilities(has_compose=True),
    )
    assert resolve_compose_project_name(proj_sq) == "single-quoted-stack"


def test_resolve_compose_project_name_ignores_comments_and_inline_comments(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        "# name: commented-out\n"
        "name: active-stack # inline comment describing stack\n"
        "services:\n"
    )
    proj = Project(
        name="fallback",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )
    assert resolve_compose_project_name(proj) == "active-stack"


def test_resolve_compose_project_name_ignores_indented_service_name(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        "services:\n"
        "  web:\n"
        "    name: ignored-service-name\n"
        "    image: nginx\n"
    )
    proj = Project(
        name="fallback-project",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )
    # Indented name is ignored; falls back to project.name
    assert resolve_compose_project_name(proj) == "fallback-project"


def test_resolve_compose_project_name_fallback_to_project_name(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services:\n  web:\n    image: nginx\n")

    proj = Project(
        name="fallback-project",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )
    assert resolve_compose_project_name(proj) == "fallback-project"


def test_resolve_compose_project_name_ignores_unsafe_declared_name(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: ../../../etc/shadow\nservices:\n")

    proj = Project(
        name="safe-fallback",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )
    assert resolve_compose_project_name(proj) == "safe-fallback"


def test_resolve_compose_project_name_non_compose_returns_none() -> None:
    proj = Project(
        name="non-compose",
        path="/tmp/non-compose",
        compose_files=[],
        capabilities=ProjectCapabilities(has_compose=False),
    )
    assert resolve_compose_project_name(proj) is None


# ---------------------------------------------------------------------------
# Unit tests: verify_container_provenance
# ---------------------------------------------------------------------------


def test_verify_container_provenance_working_dir_root(tmp_path: Path) -> None:
    proj = Project(
        name="test-app",
        path=str(tmp_path),
        compose_files=[str(tmp_path / "docker-compose.yml")],
    )
    labels = {"com.docker.compose.project.working_dir": str(tmp_path)}
    assert verify_container_provenance(labels, proj) is True


def test_verify_container_provenance_working_dir_subpath(tmp_path: Path) -> None:
    subpath = tmp_path / "supabase" / "docker"
    subpath.mkdir(parents=True)
    proj = Project(
        name="local-ai-packaged",
        path=str(tmp_path),
        compose_files=[str(tmp_path / "docker-compose.yml")],
    )
    labels = {"com.docker.compose.project.working_dir": str(subpath)}
    assert verify_container_provenance(labels, proj) is True


def test_verify_container_provenance_rejects_unrelated_working_dir(tmp_path: Path) -> None:
    proj = Project(
        name="test-app",
        path=str(tmp_path / "app"),
        compose_files=[str(tmp_path / "app" / "docker-compose.yml")],
    )
    labels = {"com.docker.compose.project.working_dir": str(tmp_path / "other-app")}
    assert verify_container_provenance(labels, proj) is False


def test_verify_container_provenance_rejects_prefix_collision(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    collision_dir = tmp_path / "app-evil"
    collision_dir.mkdir()

    proj = Project(
        name="app",
        path=str(app_dir),
        compose_files=[str(app_dir / "docker-compose.yml")],
    )
    labels = {"com.docker.compose.project.working_dir": str(collision_dir)}
    assert verify_container_provenance(labels, proj) is False


def test_verify_container_provenance_config_files_fallback(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    proj = Project(
        name="test-app",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
    )
    # When working_dir is missing, config_files inside project is trusted
    labels = {"com.docker.compose.project.config_files": str(compose_file)}
    assert verify_container_provenance(labels, proj) is True

    # When config_files points outside, it is rejected
    labels_outside = {"com.docker.compose.project.config_files": "/etc/docker/compose.yml"}
    assert verify_container_provenance(labels_outside, proj) is False


def test_verify_container_provenance_fail_closed_on_absent_labels(tmp_path: Path) -> None:
    proj = Project(
        name="test-app",
        path=str(tmp_path),
        compose_files=[str(tmp_path / "docker-compose.yml")],
    )
    assert verify_container_provenance({}, proj) is False
    assert verify_container_provenance(None, proj) is False
    assert verify_container_provenance({"com.docker.compose.project": "test-app"}, proj) is False


# ---------------------------------------------------------------------------
# Integration tests: ComposeProvider.ps
# ---------------------------------------------------------------------------


def test_compose_provider_ps_uses_resolved_identity_and_filters_provenance(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: custom-stack\nservices:\n")

    proj = Project(
        name="dir-name",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    # Valid matching container from this project
    c_valid = SimpleNamespace(
        id="c1",
        name="custom-stack-web-1",
        status="running",
        labels={
            "com.docker.compose.project": "custom-stack",
            "com.docker.compose.service": "web",
            "com.docker.compose.project.working_dir": str(tmp_path),
        },
        attrs={
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
            "Config": {"Image": "nginx:latest"},
            "NetworkSettings": {"Ports": {}},
        },
    )

    # Malicious container with matching project label but from different directory
    c_unprovenanced = SimpleNamespace(
        id="c2",
        name="rogue-web-1",
        status="running",
        labels={
            "com.docker.compose.project": "custom-stack",
            "com.docker.compose.service": "rogue",
            "com.docker.compose.project.working_dir": "/home/other/rogue-dir",
        },
        attrs={
            "State": {"Status": "running"},
            "Config": {"Image": "nginx:latest"},
            "NetworkSettings": {"Ports": {}},
        },
    )

    # Spoofed container without provenance labels
    c_spoofed = SimpleNamespace(
        id="c3",
        name="spoofed-web-1",
        status="running",
        labels={"com.docker.compose.project": "custom-stack"},
        attrs={
            "State": {"Status": "running"},
            "Config": {"Image": "nginx:latest"},
            "NetworkSettings": {"Ports": {}},
        },
    )

    mock_client = MagicMock()
    mock_client.containers.list.return_value = [c_valid, c_unprovenanced, c_spoofed]

    with patch("docker.from_env", return_value=mock_client):
        provider = ComposeProvider()
        containers = provider.ps(proj)

        # Verified that Docker client was filtered by exact resolved name
        mock_client.containers.list.assert_called_once_with(
            all=True,
            filters={"label": "com.docker.compose.project=custom-stack"},
        )
        # Verified that provenance filtering retained ONLY the legitimate container
        assert len(containers) == 1
        assert containers[0].name == "custom-stack-web-1"


# ---------------------------------------------------------------------------
# Integration tests: ProjectIntelligenceService and Safety Gate Invariant
# ---------------------------------------------------------------------------


def test_project_intelligence_delegates_to_canonical_resolver(tmp_path: Path) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("name: localai\nservices:\n")

    proj = Project(
        name="local-ai-packaged",
        path=str(tmp_path),
        compose_files=[str(compose_file)],
        capabilities=ProjectCapabilities(has_compose=True),
    )

    canonical = resolve_compose_project_name(proj)
    intel = ProjectIntelligenceService._compose_identity(proj)
    assert canonical == "localai"
    assert intel == canonical


def test_git_safety_gate_remains_blocked_for_local_ai_packaged() -> None:
    """Verifies invariant: Fixing Compose health does NOT override Git critical modification blocking."""
    planner = UpdatePlanner()
    plan = planner.plan("local-ai-packaged")
    assert plan.risk == UpdateRisk.BLOCKED
    assert plan.proceed is False
