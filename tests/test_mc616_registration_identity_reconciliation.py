"""Tests for MC-6.16 project registration & identity reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from fastapi.testclient import TestClient

from aipm.control_plane.registration import (
    ProjectRegistration,
    RegistrationStatus,
)
from aipm.services.project.identity_resolver import ProjectIdentityResolver
from aipm.capabilities.dashboard.operator_client import (
    AsgiOperatorTransportClient,
    assert_allowed_route,
)
from aipm.capabilities.dashboard.update_proxy_api import DashboardUpdateProxyApi
from aipm.control_plane.transport import create_operator_app
from aipm.dashboard.server import create_app

from tests.test_mc613_c4_composition import (
    PROJECT_ID,
    _harness,
    _register_project,
)
from tests.test_mc612_stage9_transport import SECRET, csrf_headers, login


DISCOVERY_PROJECT_ID = "f55f34521cf1e43173e51795"
CANONICAL_TARGET_ID = "local-ai-packaged"


def test_identity_resolver_mappings():
    resolver = ProjectIdentityResolver()
    assert resolver.resolve_target_id(DISCOVERY_PROJECT_ID) == CANONICAL_TARGET_ID
    assert resolver.resolve_target_id("/home/ubuntu/local-ai-packaged") == CANONICAL_TARGET_ID
    assert resolver.resolve_target_id(CANONICAL_TARGET_ID) == CANONICAL_TARGET_ID
    assert resolver.resolve_target_id("other-unknown-id") == "other-unknown-id"
    assert resolver.is_canonical_target(CANONICAL_TARGET_ID) is True
    assert resolver.is_canonical_target(DISCOVERY_PROJECT_ID) is False

    # Also test class methods directly
    assert ProjectIdentityResolver.resolve_target_id(DISCOVERY_PROJECT_ID) == CANONICAL_TARGET_ID
    assert ProjectIdentityResolver.resolve_discovery_id(CANONICAL_TARGET_ID) == DISCOVERY_PROJECT_ID
    assert ProjectIdentityResolver.is_canonical_target(CANONICAL_TARGET_ID) is True


def test_operator_client_route_allowlist_canonical_target():
    assert_allowed_route("GET", f"/projects/{CANONICAL_TARGET_ID}/registration")
    assert_allowed_route("GET", f"/projects/{DISCOVERY_PROJECT_ID}/registration")
    assert_allowed_route("GET", f"/projects/{PROJECT_ID}/registration")
    assert_allowed_route("GET", "/kill-switch")


def test_operator_registration_endpoint(tmp_path: Path):
    service, plans, _calls = _harness(tmp_path)
    registration = ProjectRegistration(
        registration_id="reg-12345",
        target_id=CANONICAL_TARGET_ID,
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/local-ai-packaged",
        runtime_mode="compose",
        compose_project_name="localai",
        registration_digest="d" * 64,
        registration_version=1,
        registered_by="owner",
        registered_at=datetime.now(timezone.utc),
    )
    service._registrations.save(registration)

    operator = create_operator_app(service)
    client = TestClient(operator)
    login(client)

    response = client.get(f"/projects/{CANONICAL_TARGET_ID}/registration")
    assert response.status_code == 200
    data = response.json()
    assert data["registered"] is True
    assert data["target_id"] == CANONICAL_TARGET_ID
    assert data["status"] == "REGISTERED"
    assert "permits_operations" in data
    assert "execution_locked" in data


def test_dashboard_proxy_registration_resolution(tmp_path: Path):
    from tests.test_mc613_c5_dashboard_proxy import _stack, _login

    dashboard, operator, service, plans, _calls = _stack(tmp_path)
    registration = ProjectRegistration(
        registration_id="reg-12345",
        target_id=CANONICAL_TARGET_ID,
        environment="production",
        status=RegistrationStatus.REGISTERED,
        canonical_project_path="/home/ubuntu/local-ai-packaged",
        runtime_mode="compose",
        compose_project_name="localai",
        registration_digest="d" * 64,
        registration_version=1,
        registered_by="owner",
        registered_at=datetime.now(timezone.utc),
    )
    service._registrations.save(registration)

    _login(operator, dashboard)

    # Calling with discovery ID should resolve to canonical target
    response = dashboard.get(f"/api/projects/{DISCOVERY_PROJECT_ID}/registration")
    assert response.status_code == 200
    res = response.json()
    assert res["status"] == "ok"
    assert res["registration"]["registered"] is True
    assert res["registration"]["target_id"] == CANONICAL_TARGET_ID
    assert "permits_operations" in res["registration"]
    assert "execution_locked" in res["registration"]


def test_frontend_static_contracts():
    js_path = Path("src/aipm/dashboard/static/mission-control-projects.js")
    source = js_path.read_text(encoding="utf-8")

    # Banned strings from MC-6.13 and MC-6.15
    for banned in ("/socket", "executor", "mutation_receipt", "/audit", "/actions/", "kill-switch"):
        assert banned not in source, f"Banned string {banned} found in frontend JS"

    assert "projectRegistrationCard" in source
    assert "registrationSection" in source
    assert "/api/projects/" in source
    assert "/registration" in source
