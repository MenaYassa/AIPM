from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from aipm.capabilities.dashboard.systemd_api import DashboardSystemdApi
from aipm.dashboard.server import create_app
from aipm.mappers.systemd import SystemdResponseMapper
from aipm.models.systemd import SYSTEMD_UNIT_REGISTRY, SystemdUnitId, SystemdUnitStatus, SystemdUnitSnapshot
from aipm.providers.systemd import LocalSystemdProvider, SystemdProviderError
from aipm.services.systemd.observation import SystemdObservationService


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def snapshot(entry, *, active="active", sub="running", enabled=True):
    return SystemdUnitSnapshot(
        id=entry.id,
        display_name=entry.display_name,
        load_state="loaded",
        active_state=active,
        sub_state=sub,
        enabled=enabled,
        status=SystemdUnitStatus.ACTIVE if active == "active" else SystemdUnitStatus.FAILED if active == "failed" else SystemdUnitStatus.INACTIVE,
    )


def test_registry_is_small_backend_owned_and_explicit() -> None:
    ids = {entry.id.value for entry in SYSTEMD_UNIT_REGISTRY}
    assert ids == {"aipm-dashboard", "aipm-telemetry", "aipm-events", "cloudflared"}
    assert all(entry.unit_name.endswith(".service") for entry in SYSTEMD_UNIT_REGISTRY)


def test_local_provider_uses_fixed_user_command_and_safe_properties() -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\n", returncode=0)

    provider = LocalSystemdProvider(runner=runner)
    entry = next(item for item in SYSTEMD_UNIT_REGISTRY if item.id == SystemdUnitId.DASHBOARD)
    result = provider.observe(entry)

    assert result.status is SystemdUnitStatus.ACTIVE
    assert calls[0][0] == ("systemctl", "--user", "show", "aipm-dashboard.service", "--no-pager", "--property=LoadState,ActiveState,SubState,UnitFileState")
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == 2.0


def test_local_provider_uses_system_scope_only_for_registry_entry() -> None:
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="LoadState=loaded\nActiveState=inactive\nSubState=dead\nUnitFileState=disabled\n", returncode=0)

    provider = LocalSystemdProvider(runner=runner)
    entry = next(item for item in SYSTEMD_UNIT_REGISTRY if item.id == SystemdUnitId.CLOUDFLARED)
    result = provider.observe(entry)
    assert calls[0][0:2] == ("systemctl", "show")
    assert result.status is SystemdUnitStatus.INACTIVE


def test_unknown_unit_id_is_rejected_before_provider_query() -> None:
    class GuardProvider:
        def observe(self, _entry):
            raise AssertionError("provider must not be called for unknown IDs")

    service = SystemdObservationService(GuardProvider(), clock=lambda: NOW)
    response = service.unit("arbitrary-host-unit")
    assert response["unit"] is None
    assert response["errors"][0]["code"] == "SYSTEMD_UNIT_NOT_ALLOWLISTED"


def test_observation_preserves_failed_unit_as_available_degraded_state() -> None:
    entry = SYSTEMD_UNIT_REGISTRY[0]

    class FailedProvider:
        def observe(self, observed_entry):
            assert observed_entry == entry
            return snapshot(entry, active="failed", sub="failed", enabled=False)

    result = SystemdObservationService(FailedProvider(), clock=lambda: NOW).unit(entry.id.value)
    assert result["observation"]["state"] == "fresh"
    assert result["observation"]["available"] is True
    assert result["unit"]["status"] == "failed"


def test_manager_failure_is_explicit_error_and_safe() -> None:
    class DownProvider:
        def observe(self, _entry):
            raise SystemdProviderError("private manager exception text")

    result = SystemdObservationService(DownProvider(), clock=lambda: NOW).units(limit=1)
    assert result["observation"]["state"] == "error"
    assert result["errors"][0]["message"] == "Systemd observation unavailable"
    assert "private manager exception text" not in str(result)


def test_provider_rejects_malformed_output_and_oversized_output() -> None:
    def malformed(_command, **_kwargs):
        return SimpleNamespace(stdout="ActiveState=active\n", returncode=0)

    entry = SYSTEMD_UNIT_REGISTRY[0]
    try:
        LocalSystemdProvider(runner=malformed).observe(entry)
    except SystemdProviderError as exc:
        assert str(exc) == "systemd response malformed"
    else:
        raise AssertionError("malformed output must fail")

    def oversized(_command, **_kwargs):
        return SimpleNamespace(stdout="LoadState=loaded\nActiveState=active\nSubState=running\n" + ("x" * 40_000), returncode=0)

    try:
        LocalSystemdProvider(runner=oversized).observe(entry)
    except SystemdProviderError as exc:
        assert str(exc) == "systemd response exceeded bounds"
    else:
        raise AssertionError("oversized output must fail")


def test_api_routes_are_get_only_bounded_and_safe() -> None:
    class FakeDashboard:
        history_api = None

        def overview(self):
            return {"available": True}

    class FakeEvents:
        def events(self, **_kwargs):
            return {"available": True, "events": []}

        def incidents(self, **_kwargs):
            return {"available": True, "incidents": []}

    class FakeNotifications:
        def notifications(self, **_kwargs): return {"available": True, "notifications": []}
        def channels(self): return {"available": True, "channels": []}
        def policies(self): return {"available": True, "policies": []}
        def metrics(self): return {"available": True, "metrics": {}}

    class FakeHealth:
        def services(self): return {"available": True, "services": {}}

    class FakeServer:
        def server(self): return {"available": True}

    class FakeDocker:
        def summary(self, **_kwargs): return {"available": True, "summary": {}, "groups": [], "containers": []}
        def containers(self, **_kwargs): return {"available": True, "containers": []}
        def container(self, _identifier): return {"available": False, "container": None}
        def images(self, **_kwargs): return {"available": True, "images": []}
        def volumes(self, **_kwargs): return {"available": True, "volumes": []}
        def networks(self, **_kwargs): return {"available": True, "networks": []}

    class FakeProject:
        def projects(self, **_kwargs): return {"available": True, "projects": [], "local_candidates": [], "filtered_candidates": []}
        def project(self, _identifier): return {"available": False, "project": None}
        def containers(self, _identifier): return {"available": False, "containers": []}
        def health(self, _identifier): return {"available": False, "health": None}

    class FakeProvider:
        def observe(self, entry): return snapshot(entry)

    systemd_api = DashboardSystemdApi(SystemdObservationService(FakeProvider(), clock=lambda: NOW), SystemdResponseMapper())
    client = TestClient(create_app(application=object(), dashboard_api=FakeDashboard(), incidents_api=FakeEvents(), notifications_api=FakeNotifications(), service_health_api=FakeHealth(), server_api=FakeServer(), docker_api=FakeDocker(), project_api=FakeProject(), systemd_api=systemd_api))

    response = client.get("/api/systemd/units?limit=1")
    assert response.status_code == 200
    assert len(response.json()["units"]) == 1
    assert client.get("/api/systemd/units/arbitrary").status_code == 200
    assert client.post("/api/systemd/units").status_code == 405
    assert client.put("/api/systemd/units/aipm-dashboard").status_code == 405
    assert client.get("/api/systemd/units?limit=21").status_code == 422
    text = response.text
    for forbidden in ("ExecStart", "environment", "private manager", "systemctl", "command"):
        assert forbidden not in text


def test_frontend_systemd_contract_is_static_mounted_and_action_free() -> None:
    from pathlib import Path

    static = Path(__file__).parents[1] / "src/aipm/dashboard/static"
    html = (static / "index.html").read_text(encoding="utf-8")
    module = (static / "mission-control-systemd.js").read_text(encoding="utf-8")
    assert "/static/mission-control-systemd.js" in html
    for marker in ("Systemd Observation", "systemdObservationState", "systemdUnits", "systemdDetail", "scheduler.register('systemd'", "observation-only"):
        assert marker in html
    for marker in ("/api/systemd/units?limit=20", "/api/systemd/units/", "createSystemdController", "Health", "Evidence"):
        assert marker in module
    lowered = module.lower()
    for forbidden in ("setinterval", "start button", "systemctl", "subprocess", "method=\"post\"", "docker start", "git pull"):
        assert forbidden not in lowered
