"""Tests for MC-6.15-A.3: Public Compose Service Candidate Intelligence API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from aipm.capabilities.dashboard.project_api import DashboardProjectApi
from aipm.capabilities.dashboard.safety import scan_payload
from aipm.dashboard.server import create_app
from aipm.models.compose_intelligence import (
    CandidateLookupKey,
    ComposeProjectObservation,
    ComposeServiceObservation,
    ServiceCandidateReason,
    ServiceCandidateStatus,
)
from aipm.models.project import Project, ProjectCapabilities
from aipm.services.compose.service import ComposeService
from aipm.services.project.intelligence import ProjectIntelligenceService


@dataclass
class FakeDetail:
    id: str
    name: str
    project_key: str | None
    service_name: str | None
    image: str
    state: str
    health: str | None
    restart_count: int = 0
    resources: object | None = None
    ports: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    mount_kinds: tuple[str, ...] = ()
    started_at: str | None = None


class FakeProjectService:
    def __init__(self, projects: list[Project]) -> None:
        self.projects = projects
        self.app = SimpleNamespace(config=SimpleNamespace(discovery=SimpleNamespace(search_paths=["/srv/projects"])))

    def discover(self):
        return self.projects

    def get_project(self, name: str) -> Project:
        for p in self.projects:
            if p.name == name:
                return p
        raise LookupError(f"Project '{name}' not found")


class FakeTelemetry:
    def __init__(self, details: list[FakeDetail]) -> None:
        self.details = details

    def fast_snapshot(self, *, now):
        items = [SimpleNamespace(container=item, resources=None) for item in self.details]
        return SimpleNamespace(containers=items, state_sampled_at=now)


class FakeObservation:
    def containers(self):
        return []


def create_test_fixture():
    now = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)
    key_ollama = CandidateLookupKey(
        registry="docker.io",
        repository="ollama/ollama",
        tag="latest",
        target_os="linux",
        target_arch="arm64",
    )
    key_litellm = CandidateLookupKey(
        registry="ghcr.io",
        repository="berriai/litellm",
        tag="main-stable",
        target_os="linux",
        target_arch="arm64",
    )

    svc_ollama = ComposeServiceObservation(
        service_name="ollama",
        container_names=("localai-ollama-1",),
        container_ids=("351e939dc4e6",),
        state="running",
        health="healthy",
        declared_image="ollama/ollama:latest",
        declared_image_ref=None,
        running_image="ollama/ollama:latest",
        running_image_id="sha256:ollama_img_id",
        running_repo_digests=("ollama/ollama@sha256:current_digest",),
        candidate_digest="sha256:new_ollama_digest",
        candidate_child_digest="sha256:new_ollama_child",
        candidate_status=ServiceCandidateStatus.UPDATE_AVAILABLE,
        candidate_reason=ServiceCandidateReason.CANDIDATE_DIGEST_DIFFERS,
        candidate_detail="Remote registry contains newer manifest digest",
        is_build=False,
        build_context=None,
        build_dockerfile=None,
        ports=("11434:11434",),
        freshness="fresh",
        observed_at=now,
        provenance_verified=True,
        depends_on=(),
        candidate_key=key_ollama,
    )

    svc_litellm = ComposeServiceObservation(
        service_name="litellm",
        container_names=("localai-litellm-1",),
        container_ids=("7bde0564da2a",),
        state="running",
        health="healthy",
        declared_image="ghcr.io/berriai/litellm:main-stable",
        declared_image_ref=None,
        running_image="ghcr.io/berriai/litellm:main-stable",
        running_image_id="sha256:litellm_img_id",
        running_repo_digests=("ghcr.io/berriai/litellm@sha256:litellm_digest",),
        candidate_digest="sha256:litellm_digest",
        candidate_child_digest=None,
        candidate_status=ServiceCandidateStatus.CURRENT,
        candidate_reason=ServiceCandidateReason.UP_TO_DATE,
        candidate_detail="Running digest matches remote candidate",
        is_build=False,
        build_context=None,
        build_dockerfile=None,
        ports=("4001:4000",),
        freshness="fresh",
        observed_at=now,
        provenance_verified=True,
        depends_on=("ollama",),
        candidate_key=key_litellm,
    )

    svc_custom = ComposeServiceObservation(
        service_name="custom-build",
        container_names=("localai-custom-1", "localai-custom-2"),
        container_ids=("cid1", "cid2"),
        state="running",
        health=None,
        declared_image=None,
        declared_image_ref=None,
        running_image="localai-custom:latest",
        running_image_id="sha256:custom_id",
        running_repo_digests=(),
        candidate_digest=None,
        candidate_child_digest=None,
        candidate_status=ServiceCandidateStatus.NOT_APPLICABLE,
        candidate_reason=ServiceCandidateReason.LOCAL_BUILD,
        candidate_detail="Service specifies build section; candidate tracking not applicable",
        is_build=True,
        build_context="/path/to/build",
        build_dockerfile="Dockerfile",
        ports=(),
        freshness="fresh",
        observed_at=now,
        provenance_verified=False,
        depends_on=("litellm",),
        candidate_key=None,
    )

    obs = ComposeProjectObservation(
        project_name="local-ai-packaged",
        compose_identity="local-ai-packaged",
        project_path="/srv/projects/local-ai-packaged",
        compose_files=("/srv/projects/local-ai-packaged/docker-compose.yml",),
        services=(svc_ollama, svc_litellm, svc_custom),
        running_services_count=3,
        total_services_count=3,
        updates_available_count=1,
        current_count=1,
        drift_count=0,
        not_applicable_count=1,
        unknown_count=0,
        observed_at=now,
        freshness="fresh",
    )

    project_compose = Project(
        name="local-ai-packaged",
        path="/srv/projects/local-ai-packaged",
        capabilities=ProjectCapabilities(has_compose=True),
        compose_files=["/srv/projects/local-ai-packaged/docker-compose.yml"],
    )
    project_non_compose = Project(
        name="invoicing",
        path="/srv/projects/invoicing",
        capabilities=ProjectCapabilities(has_compose=False, has_git=True),
        compose_files=[],
    )

    details = [
        FakeDetail("351e939dc4e6", "ollama", "local-ai-packaged", "ollama", "ollama/ollama:latest", "running", "healthy"),
        FakeDetail("7bde0564da2a", "litellm", "local-ai-packaged", "litellm", "ghcr.io/berriai/litellm:main-stable", "running", "healthy"),
    ]

    mock_intelligence = MagicMock()
    mock_intelligence.observe.return_value = obs

    compose_svc = ComposeService(intelligence=mock_intelligence)
    project_svc = FakeProjectService([project_compose, project_non_compose])
    intel_svc = ProjectIntelligenceService(
        project_svc,
        FakeObservation(),
        FakeTelemetry(details),
        compose_service=compose_svc,
    )
    dashboard_api = DashboardProjectApi(intel_svc)
    app = create_app(project_api=dashboard_api)
    client = TestClient(app)

    # Resolve project IDs
    inventory = intel_svc.inventory()
    compose_id = next(p.id for p in inventory.projects if p.display_name == "local-ai-packaged")
    non_compose_id = next(p.id for p in inventory.local_candidates if p.display_name == "invoicing")

    return {
        "client": client,
        "compose_id": compose_id,
        "non_compose_id": non_compose_id,
        "mock_intelligence": mock_intelligence,
        "observation": obs,
        "dashboard_api": dashboard_api,
    }


def test_1_route_returns_200_for_valid_compose_project():
    fix = create_test_fixture()
    client = fix["client"]
    res = client.get(f"/api/projects/{fix['compose_id']}/compose-intelligence")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    assert body["status"] == "ok"
    assert body["error"] is None
    assert body["observation"]["available"] is True
    assert body["observation"]["state"] == "fresh"

    proj = body["project"]
    assert proj["id"] == fix["compose_id"]
    assert proj["display_name"] == "local-ai-packaged"
    assert proj["compose_identity"] == "local-ai-packaged"
    assert proj["running_services_count"] == 3
    assert proj["total_services_count"] == 3
    assert proj["updates_available_count"] == 1
    assert proj["current_count"] == 1
    assert proj["not_applicable_count"] == 1
    assert len(proj["services"]) == 3


def test_2_invalid_project_id_fails_closed():
    fix = create_test_fixture()
    client = fix["client"]

    # Malformed / traversal ID
    res = client.get("/api/projects/invalid-hex-id/compose-intelligence")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False
    assert body["status"] == "error"
    assert body["error"] == "Project identifier is invalid"
    assert body["project"] is None

    # Unknown 24-hex ID
    res = client.get("/api/projects/ffffffffffffffffffffffff/compose-intelligence")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False
    assert body["status"] == "error"
    assert body["error"] == "Project is unavailable"
    assert body["project"] is None


def test_3_non_compose_project_returns_bounded_unavailable_behavior():
    fix = create_test_fixture()
    client = fix["client"]
    res = client.get(f"/api/projects/{fix['non_compose_id']}/compose-intelligence")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False
    assert body["status"] == "unavailable"
    assert "unavailable" in body["error"].lower()
    assert body["project"] is None
    assert body["observation"]["state"] == "unavailable"


def test_4_service_statuses_are_preserved_exactly():
    fix = create_test_fixture()
    client = fix["client"]
    res = client.get(f"/api/projects/{fix['compose_id']}/compose-intelligence")
    body = res.json()
    by_svc = {s["service_name"]: s for s in body["project"]["services"]}

    assert by_svc["litellm"]["candidate_status"] == "current"
    assert by_svc["litellm"]["candidate_reason"] == "up_to_date"

    assert by_svc["ollama"]["candidate_status"] == "update_available"
    assert by_svc["ollama"]["candidate_reason"] == "candidate_digest_differs"

    assert by_svc["custom-build"]["candidate_status"] == "not_applicable"
    assert by_svc["custom-build"]["candidate_reason"] == "local_build"


def test_5_candidate_digest_fields_are_preserved():
    fix = create_test_fixture()
    client = fix["client"]
    res = client.get(f"/api/projects/{fix['compose_id']}/compose-intelligence")
    body = res.json()
    by_svc = {s["service_name"]: s for s in body["project"]["services"]}

    ollama = by_svc["ollama"]
    assert ollama["candidate_digest"] == "sha256:new_ollama_digest"
    assert ollama["candidate_child_digest"] == "sha256:new_ollama_child"
    assert ollama["running_repo_digests"] == ["ollama/ollama@sha256:current_digest"]
    assert ollama["running_image_id"] == "sha256:ollama_img_id"


def test_6_candidate_lookup_key_is_normalized():
    fix = create_test_fixture()
    client = fix["client"]
    res = client.get(f"/api/projects/{fix['compose_id']}/compose-intelligence")
    body = res.json()
    by_svc = {s["service_name"]: s for s in body["project"]["services"]}

    ollama_key = by_svc["ollama"]["candidate_key"]
    assert ollama_key == {
        "registry": "docker.io",
        "repository": "ollama/ollama",
        "tag": "latest",
        "os": "linux",
        "arch": "arm64",
    }

    litellm_key = by_svc["litellm"]["candidate_key"]
    assert litellm_key == {
        "registry": "ghcr.io",
        "repository": "berriai/litellm",
        "tag": "main-stable",
        "os": "linux",
        "arch": "arm64",
    }

    assert by_svc["custom-build"]["candidate_key"] is None


def test_7_depends_on_is_preserved():
    fix = create_test_fixture()
    client = fix["client"]
    res = client.get(f"/api/projects/{fix['compose_id']}/compose-intelligence")
    body = res.json()
    by_svc = {s["service_name"]: s for s in body["project"]["services"]}

    assert by_svc["ollama"]["depends_on"] == []
    assert by_svc["litellm"]["depends_on"] == ["ollama"]
    assert by_svc["custom-build"]["depends_on"] == ["litellm"]


def test_8_provenance_verified_is_preserved():
    fix = create_test_fixture()
    client = fix["client"]
    res = client.get(f"/api/projects/{fix['compose_id']}/compose-intelligence")
    body = res.json()
    by_svc = {s["service_name"]: s for s in body["project"]["services"]}

    assert by_svc["ollama"]["provenance_verified"] is True
    assert by_svc["custom-build"]["provenance_verified"] is False


def test_9_multi_container_service_aggregation_is_preserved():
    fix = create_test_fixture()
    client = fix["client"]
    res = client.get(f"/api/projects/{fix['compose_id']}/compose-intelligence")
    body = res.json()
    by_svc = {s["service_name"]: s for s in body["project"]["services"]}

    custom = by_svc["custom-build"]
    assert custom["container_names"] == ["localai-custom-1", "localai-custom-2"]
    assert custom["container_ids"] == ["cid1", "cid2"]


def test_10_registry_credentials_are_never_serialized():
    fix = create_test_fixture()
    client = fix["client"]
    res = client.get(f"/api/projects/{fix['compose_id']}/compose-intelligence")
    body = res.json()
    findings = scan_payload(body)
    assert len(findings) == 0, f"Unsafe payload findings: {findings}"

    # Verify no credential-like substrings in raw text
    text = res.text.lower()
    for forbidden in ("password", "bearer", "authorization", "secret", "token"):
        assert forbidden not in text


def test_11_raw_docker_attrs_are_never_serialized():
    fix = create_test_fixture()
    client = fix["client"]
    res = client.get(f"/api/projects/{fix['compose_id']}/compose-intelligence")
    body = res.json()

    text = res.text
    assert "HostConfig" not in text
    assert "NetworkSettings" not in text
    assert "GraphDriver" not in text
    assert "build_context" not in text  # Internal filesystem path omitted
    assert "project_path" not in text   # Internal filesystem path omitted


def test_12_no_mutation_endpoints_are_reachable_through_new_route():
    fix = create_test_fixture()
    client = fix["client"]
    cid = fix["compose_id"]

    assert client.post(f"/api/projects/{cid}/compose-intelligence").status_code == 405
    assert client.put(f"/api/projects/{cid}/compose-intelligence").status_code == 405
    assert client.delete(f"/api/projects/{cid}/compose-intelligence").status_code == 405
    assert client.patch(f"/api/projects/{cid}/compose-intelligence").status_code == 405


def test_13_mocked_compose_intelligence_service_proves_dashboard_uses_existing_service():
    fix = create_test_fixture()
    client = fix["client"]
    mock_intelligence = fix["mock_intelligence"]

    assert mock_intelligence.observe.call_count == 0
    res = client.get(f"/api/projects/{fix['compose_id']}/compose-intelligence")
    assert res.status_code == 200
    assert mock_intelligence.observe.call_count == 1
    call_args, call_kwargs = mock_intelligence.observe.call_args
    assert call_kwargs.get("query_registries") is True


def test_14_response_is_bounded():
    fix = create_test_fixture()
    client = fix["client"]
    res = client.get(f"/api/projects/{fix['compose_id']}/compose-intelligence")
    assert len(res.content) < 65536  # Bounded response payload


def test_15_deterministic_serialization_order():
    fix = create_test_fixture()
    client = fix["client"]
    res = client.get(f"/api/projects/{fix['compose_id']}/compose-intelligence")
    body = res.json()
    services = body["project"]["services"]
    names = [s["service_name"] for s in services]
    assert names == sorted(names)
