from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from aipm.core.exceptions import DockerError
from aipm.mappers.dashboard import DashboardResponseMapper
from aipm.models.container import Container
from aipm.models.project import Project
from aipm.models.telemetry import (
    ContainerSnapshot,
    DockerSnapshot,
    HandbookRoute,
    ResourceStats,
)
from aipm.services.docker.service import DockerService
from aipm.services.telemetry.dashboard import DashboardTelemetryService
from aipm.services.telemetry.docker import DockerTelemetryService
from aipm.services.telemetry.host import HostTelemetryService
from aipm.services.telemetry.project import ProjectTelemetryService
from aipm.services.telemetry.tunnel import TunnelTelemetryService


@dataclass
class FakeSystemService:
    value: object

    def summary(self):
        return self.value


class FakePsutil:
    def swap_memory(self):
        return SimpleNamespace(total=1024**3, used=128 * 1024**2, percent=12.5)

    def boot_time(self):
        return 900.0

    def net_if_addrs(self):
        return {"eth0": []}

    def net_connections(self, kind):
        return [SimpleNamespace(status="ESTABLISHED"), SimpleNamespace(status="TIME_WAIT")]


class FakeOS:
    def getloadavg(self):
        return 1.0, 0.5, 0.25


class FakeTime:
    def time(self):
        return 1000.0


class FakeContainer:
    def __init__(self, name="app", status="running"):
        self.short_id = "abc123"
        self.name = name
        self.status = status
        self.image = SimpleNamespace(tags=["example/app:latest"])
        self.labels = {"com.docker.compose.project": "example"}
        self.ports = {"8080/tcp": [{"HostPort": "8080"}]}
        self.attrs = {
            "Created": "2026-08-16T00:00:00Z",
            "State": {
                "Status": status,
                "Health": {"Status": "healthy"},
                "RestartCount": 2,
                "StartedAt": "2026-08-16T00:00:00Z",
            },
            "Config": {"Labels": self.labels},
        }


class FakeDockerProvider:
    def __init__(self, containers=None, stats_error=False, list_error=False):
        self.containers = containers or [FakeContainer()]
        self.stats_error = stats_error
        self.list_error = list_error
        self.stats_calls = 0

    def list_containers(self):
        if self.list_error:
            raise DockerError("unavailable")
        return self.containers

    def stats(self, container):
        self.stats_calls += 1
        if self.stats_error:
            raise DockerError("stats unavailable")
        return {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 200},
                "system_cpu_usage": 2000,
                "online_cpus": 2,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 100},
                "system_cpu_usage": 1000,
            },
            "memory_stats": {
                "usage": 20 * 1024**2,
                "limit": 100 * 1024**2,
                "stats": {"inactive_file": 0},
            },
        }


class FakeProjectService:
    def __init__(self, projects=None, error=None):
        self.app = SimpleNamespace(config=SimpleNamespace(discovery=SimpleNamespace(search_paths=["/srv/projects"])))
        self.projects = projects or [Project(name="demo", path="/srv/projects/demo")]
        self.error = error
        self.calls = 0

    def discover(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.projects


def test_host_telemetry_uses_existing_system_service_and_is_read_only():
    system = SimpleNamespace(
        host=SimpleNamespace(hostname="host", os="Linux", kernel="kernel", architecture="x86_64", python="3.12"),
        cpu=SimpleNamespace(physical_cores=1, logical_cores=2, usage_percent=10.0),
        memory=SimpleNamespace(total_gb=4.0, used_gb=1.0, available_gb=3.0, percent=25.0),
        disk=SimpleNamespace(total_gb=20.0, used_gb=5.0, free_gb=15.0, percent=25.0),
    )
    snapshot = HostTelemetryService(
        system_service=FakeSystemService(system),
        psutil_module=FakePsutil(),
        os_module=FakeOS(),
        time_module=FakeTime(),
    ).snapshot()
    assert snapshot.system is system
    assert snapshot.swap.used_gb == 0.12
    assert snapshot.load_one == 1.0
    assert snapshot.uptime_seconds == 100
    assert snapshot.network.established == 1


def test_docker_available_maps_typed_container_snapshot():
    provider = FakeDockerProvider()
    snapshot = DockerTelemetryService(DockerService(provider=provider)).snapshot()
    assert snapshot.available is True
    assert snapshot.running == 1
    assert snapshot.containers[0].container.name == "app"
    assert snapshot.containers[0].resources.memory_used_mb == 20.0
    assert provider.stats_calls == 1


def test_docker_unavailable_does_not_break_snapshot_contract():
    snapshot = DockerTelemetryService(DockerService(provider=FakeDockerProvider(list_error=True))).snapshot()
    assert snapshot.available is False
    assert snapshot.containers == ()
    assert snapshot.error.message == "Docker telemetry unavailable"


def test_container_stats_failure_preserves_container():
    snapshot = DockerTelemetryService(DockerService(provider=FakeDockerProvider(stats_error=True))).snapshot()
    assert snapshot.available is True
    assert len(snapshot.containers) == 1
    assert snapshot.containers[0].container.name == "app"
    assert snapshot.containers[0].resources.available is False
    assert snapshot.containers[0].resources.error.message == "Container resource telemetry unavailable"


def test_tunnel_detected_through_docker():
    container = Container(
        id="tunnel",
        name="cloudflared",
        image="cloudflared:latest",
        state="running",
        health=None,
        ports=[],
        labels={},
        stack=None,
        created=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    docker = DockerSnapshot(
        available=True,
        status="healthy",
        containers=(ContainerSnapshot(container=container),),
    )
    result = TunnelTelemetryService(command_runner=lambda *args, **kwargs: SimpleNamespace(stdout="", stderr="", returncode=1)).snapshot(docker)
    assert result.state == "healthy"
    assert result.source == "docker"


def test_tunnel_detected_through_systemd():
    docker = DockerSnapshot(available=True, status="healthy")
    runner = lambda *args, **kwargs: SimpleNamespace(stdout="active\n", stderr="", returncode=0)
    result = TunnelTelemetryService(command_runner=runner).snapshot(docker)
    assert result.state == "healthy"
    assert result.source == "systemd"


def test_tunnel_not_detected_is_unknown():
    docker = DockerSnapshot(available=False, status="unknown")
    runner = lambda *args, **kwargs: SimpleNamespace(stdout="", stderr="Unit cloudflared.service could not be found.", returncode=4)
    result = TunnelTelemetryService(command_runner=runner).snapshot(docker)
    assert result.state == "unknown"
    assert result.source == "not-detected"


def test_project_discovery_success_and_failure():
    success_service = FakeProjectService()
    success = ProjectTelemetryService(success_service).snapshot()
    assert success.available is True
    assert success.projects[0].project.name == "demo"
    assert success_service.calls == 1

    failure = ProjectTelemetryService(FakeProjectService(error=RuntimeError("discovery failed"))).snapshot()
    assert failure.available is False
    assert failure.error.message == "Project discovery unavailable"


def test_docker_outage_does_not_break_host_telemetry():
    system = SimpleNamespace(
        host=SimpleNamespace(hostname="host", os="Linux", kernel="kernel", architecture="x86_64", python="3.12"),
        cpu=SimpleNamespace(physical_cores=1, logical_cores=2, usage_percent=10.0),
        memory=SimpleNamespace(total_gb=4.0, used_gb=1.0, available_gb=3.0, percent=25.0),
        disk=SimpleNamespace(total_gb=20.0, used_gb=5.0, free_gb=15.0, percent=25.0),
    )
    snapshot = DashboardTelemetryService(
        host=HostTelemetryService(
            system_service=FakeSystemService(system),
            psutil_module=FakePsutil(),
            os_module=FakeOS(),
            time_module=FakeTime(),
        ),
        docker=DockerTelemetryService(DockerService(provider=FakeDockerProvider(list_error=True))),
        projects=ProjectTelemetryService(FakeProjectService()),
        tunnel=TunnelTelemetryService(command_runner=lambda *args, **kwargs: SimpleNamespace(stdout="", stderr="", returncode=1)),
    ).snapshot()
    assert snapshot.host.available is True
    assert snapshot.docker.available is False


def test_dashboard_aggregation_isolates_component_failures():
    class BrokenHost:
        def snapshot(self):
            raise RuntimeError("host failed")

    class BrokenTunnel:
        def snapshot(self, docker):
            raise RuntimeError("tunnel failed")

    docker_service = DockerTelemetryService(DockerService(provider=FakeDockerProvider()))
    service = DashboardTelemetryService(
        host=BrokenHost(),
        docker=docker_service,
        projects=ProjectTelemetryService(FakeProjectService(error=RuntimeError("projects failed"))),
        tunnel=BrokenTunnel(),
        handbook=(HandbookRoute("test", "Test", "Test route", ("true",)),),
    )
    snapshot = service.snapshot()
    assert snapshot.host.available is False
    assert snapshot.docker.available is True
    assert snapshot.projects.available is False
    assert snapshot.tunnel.state == "unknown"


def test_architecture_boundaries_keep_fastapi_free_of_infrastructure_logic():
    server_source = Path("src/aipm/dashboard/server.py").read_text()
    assert "DockerProvider" not in server_source
    assert "ProjectService" not in server_source
    assert "psutil" not in server_source
    assert "subprocess" not in server_source
    assert "git fetch" not in server_source.lower()
    assert "git pull" not in server_source.lower()


def test_response_mapper_hides_raw_exception_text_and_preserves_frontend_contract():
    docker = DockerTelemetryService(DockerService(provider=FakeDockerProvider(list_error=True))).snapshot()
    mapped = DashboardResponseMapper()
    snapshot = DashboardTelemetryService(
        host=HostTelemetryService(
            system_service=FakeSystemService(SimpleNamespace(host=None, cpu=None, memory=None, disk=None)),
            psutil_module=FakePsutil(),
            os_module=FakeOS(),
            time_module=FakeTime(),
        ),
        docker=type("DockerTelemetry", (), {"snapshot": lambda self: docker})(),
        projects=ProjectTelemetryService(FakeProjectService()),
        tunnel=TunnelTelemetryService(command_runner=lambda *args, **kwargs: SimpleNamespace(stdout="", stderr="", returncode=1)),
    ).snapshot()
    response = mapped.to_response(snapshot)
    assert {"generated_at", "host", "docker", "tunnel", "projects", "handbook"} <= response.keys()
    assert response["docker"]["error"] == "Docker telemetry unavailable"
    assert "unavailable" in response["docker"]["error"].lower()
