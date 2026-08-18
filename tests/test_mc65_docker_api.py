from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from aipm.capabilities.dashboard.docker_api import DashboardDockerApi
from aipm.dashboard.server import create_app
from aipm.services.docker.observation import DockerObservationService
from aipm.services.docker.service import DockerService
from aipm.services.telemetry.docker import DockerTelemetryService


class GuardProvider:
    def __init__(self) -> None:
        self.lifecycle_calls: list[str] = []
        self.raw = _container()

    def list_containers(self):
        return [self.raw]

    def inspect(self, identifier: str):
        assert identifier in {self.raw.name, self.raw.short_id}
        return self.raw

    def images(self):
        return [_image()]

    def volumes(self):
        return [_volume()]

    def networks(self):
        return [_network()]

    def stats(self, _container):
        self.lifecycle_calls.append("stats")
        raise AssertionError("fast Docker detail must not call per-container stats")

    def stats_all(self, **_kwargs):
        self.lifecycle_calls.append("stats_all")
        raise AssertionError("MC-6.5 detail must not run aggregate refresh directly")

    def start(self, _name):
        self.lifecycle_calls.append("start")
        raise AssertionError("Docker lifecycle method reached")

    def stop(self, _name):
        self.lifecycle_calls.append("stop")
        raise AssertionError("Docker lifecycle method reached")

    def restart(self, _name):
        self.lifecycle_calls.append("restart")
        raise AssertionError("Docker lifecycle method reached")


def _container():
    return SimpleNamespace(
        id="sha256:container-full-id",
        short_id="container-1234",
        name="aipm-api",
        image=SimpleNamespace(tags=["aipm/api:latest"]),
        labels={"com.docker.compose.project": "aipm", "com.docker.compose.service": "api"},
        ports={"8787/tcp": None},
        attrs={
            "Created": "2026-08-18T10:00:00Z",
            "State": {"Status": "running", "Health": {"Status": "healthy"}, "RestartCount": 2, "StartedAt": "2026-08-18T10:01:00Z"},
            "NetworkSettings": {"Networks": {"aipm_default": {"IPAddress": "172.18.0.4"}}},
            "Config": {"Cmd": ["private-command", "--token", "PRIVATE_TOKEN"], "Env": ["PRIVATE_ENV=secret"]},
            "Mounts": [{"Type": "volume", "Source": "/private/host/path", "Name": "aipm_data"}],
        },
    )


def _image():
    return SimpleNamespace(short_id="sha256:image-123456789", tags=["aipm/api:latest"], attrs={"Size": 1024 * 1024, "Created": "2026-08-18T10:00:00Z"})


def _volume():
    return SimpleNamespace(name="aipm_data", attrs={"Driver": "local", "Scope": "local", "Mountpoint": "/private/mount"})


def _network():
    return SimpleNamespace(name="aipm_default", attrs={"Driver": "bridge", "Scope": "local", "Containers": {"private": {"IPv4Address": "172.18.0.4/16"}}})


def _api(provider: GuardProvider) -> DashboardDockerApi:
    service = DockerService(provider=provider)
    telemetry = DockerTelemetryService(service, clock=lambda: datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc))
    return DashboardDockerApi(
        telemetry,
        DockerObservationService(service),
        clock=lambda: datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc),
    )


def test_summary_groups_containers_and_preserves_resource_freshness() -> None:
    provider = GuardProvider()
    response = _api(provider).summary()

    assert response["available"] is True
    assert response["summary"] == {"total": 1, "running": 1, "stopped": 0, "unhealthy": 0}
    assert response["groups"] == [{"project_key": "aipm", "total": 1, "running": 1, "unhealthy": 0}]
    assert response["containers"][0]["resources"]["freshness"]["status"] == "never_sampled"
    assert provider.lifecycle_calls == []


def test_container_detail_is_redacted_and_does_not_expose_private_inspect_fields() -> None:
    provider = GuardProvider()
    response = _api(provider).container("container-1234")
    text = str(response)

    assert response["available"] is True
    assert response["container"]["networks"] == ["aipm_default"]
    assert response["container"]["mount_kinds"] == ["volume"]
    for forbidden in ("private-command", "PRIVATE_TOKEN", "PRIVATE_ENV", "/private/host/path", "172.18.0.4", "ip_address", "command", "mounts"):
        assert forbidden not in text
    assert provider.lifecycle_calls == []


def test_inventory_routes_are_bounded_and_safe() -> None:
    provider = GuardProvider()
    api = _api(provider)

    assert api.images(limit=1)["images"][0]["id"] == "image-123456"
    assert api.volumes(limit=1)["volumes"][0]["name"] == "aipm_data"
    assert api.networks(limit=1)["networks"][0]["driver"] == "bridge"
    assert provider.lifecycle_calls == []


def test_dashboard_docker_routes_are_additive_get_only() -> None:
    provider = GuardProvider()
    docker_api = _api(provider)

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
        def notifications(self, **_kwargs):
            return {"available": True, "notifications": []}

        def channels(self):
            return {"available": True, "channels": []}

        def policies(self):
            return {"available": True, "policies": []}

        def metrics(self):
            return {"available": True, "metrics": {}}

    class FakeHealth:
        def services(self):
            return {"available": True, "services": {}}

    class FakeServer:
        def server(self):
            return {"available": True}

    client = TestClient(create_app(
        application=object(),
        dashboard_api=FakeDashboard(),
        incidents_api=FakeEvents(),
        notifications_api=FakeNotifications(),
        service_health_api=FakeHealth(),
        server_api=FakeServer(),
        docker_api=docker_api,
    ))

    for path in ("/api/docker/summary", "/api/docker/containers", "/api/docker/containers/container-1234", "/api/docker/images", "/api/docker/volumes", "/api/docker/networks"):
        assert client.get(path).status_code == 200
        assert client.post(path).status_code == 405
    assert provider.lifecycle_calls == []
