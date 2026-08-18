from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from aipm.capabilities.dashboard.server_api import DashboardServerApi
from aipm.capabilities.dashboard.service_health_api import DashboardServiceHealthApi
from aipm.capabilities.dashboard.safety import scan_payload
from aipm.dashboard.server import create_app
from aipm.mappers.server import ServerResponseMapper
from aipm.models.server import FilesystemDetail, NetworkInterfaceDetail, ServerHostSnapshot
from aipm.models.telemetry import (
    HostSnapshot,
    NetworkStats,
    SwapStats,
    TelemetryError,
)
from aipm.models.mission_control import Observation, ObservationError, ObservationState
from aipm.models.system import SystemSummary
from aipm.models.host import HostInfo
from aipm.models.cpu import CpuInfo
from aipm.models.memory import MemoryInfo
from aipm.models.disk import DiskInfo
from aipm.services.telemetry.host import HostTelemetryService


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def host_snapshot(*, available: bool = True, error: TelemetryError | None = None) -> HostSnapshot:
    system = SystemSummary(
        host=HostInfo("test-host", "Linux", "6.1", "x86_64", "3.12"),
        cpu=CpuInfo(2, 4, 12.5),
        memory=MemoryInfo(16.0, 5.0, 11.0, 31.25),
        disk=DiskInfo(100.0, 40.0, 60.0, 40.0),
    ) if available else None
    return HostSnapshot(
        system=system,
        swap=SwapStats(2.0, 0.1, 5.0, available=available, error=error),
        load_one=0.42 if available else None,
        load_five=0.38 if available else None,
        load_fifteen=0.31 if available else None,
        uptime_seconds=12345 if available else None,
        network=NetworkStats(2, 3 if available else None, available=available, error=error),
        available=available,
        error=error,
    )


def server_snapshot(*, available: bool = True, error: TelemetryError | None = None) -> ServerHostSnapshot:
    return ServerHostSnapshot(
        host=host_snapshot(available=available, error=error),
        filesystems=(FilesystemDetail("/", "ext4", 100.0, 40.0, 60.0, 40.0),) if available else (),
        filesystem_available=available,
        filesystem_error=None if available else error,
        interfaces=(NetworkInterfaceDetail("eth0", True, 100, 200),) if available else (),
        interface_detail_available=available,
        interface_detail_error=None if available else error,
        connection_states=(("ESTABLISHED", 3), ("LISTEN", 1)) if available else (),
        connection_states_available=available,
        connection_states_error=None if available else error,
    )


class FakeHost:
    def __init__(self, snapshot: ServerHostSnapshot | None = None, failure: Exception | None = None):
        self.snapshot_value = snapshot or server_snapshot()
        self.failure = failure

    def server_snapshot(self):
        if self.failure:
            raise self.failure
        return self.snapshot_value


class FakeHealth:
    def __init__(self, payload=None):
        self.payload = payload or {"available": True, "status": "ok", "overall": "healthy", "services": {"telemetry": {"state": "fresh"}, "mc3": {"state": "fresh"}}}

    def services(self):
        return self.payload


class FakeIncidents:
    def __init__(self, payload=None):
        self.payload = payload or {"available": True, "incidents": [{"id": 1}]}

    def incidents(self, **_filters):
        return self.payload


class FakeHistory:
    def host(self, range_name, limit):
        return {"available": True, "status": "ok", "error": None, "points": [{"range": range_name, "limit": limit, "cpu_percent": 12.5}]}


class FakeOverview:
    history_api = FakeHistory()

    def overview(self):
        return {"generated_at": NOW.isoformat(), "host": {"available": True}}


class FakeNotifications:
    def notifications(self, **_kwargs):
        return {"available": True, "notifications": []}

    def channels(self):
        return {"available": True, "channels": []}

    def policies(self):
        return {"available": True, "policies": []}

    def metrics(self):
        return {"available": True, "metrics": {}}


def api_for(host, health=None, incidents=None):
    return DashboardServerApi(
        host,
        ServerResponseMapper(),
        service_health_api=health or FakeHealth(),
        incidents_api=incidents or FakeIncidents(),
        clock=lambda: NOW,
    )


def test_server_success_schema_and_bounded_detail() -> None:
    payload = api_for(FakeHost()).server()
    assert payload["available"] is True
    assert payload["status"] == "ok"
    assert payload["observation"]["state"] == "fresh"
    assert payload["identity"]["hostname"] == "test-host"
    assert payload["cpu"]["load"]["one"] == 0.42
    assert payload["memory"]["percent"] == 31.25
    assert payload["disk"]["root"]["path"] == "/"
    assert payload["network"]["established"] == 3
    assert payload["network"]["interfaces"][0]["rx_bytes"] == 100
    assert payload["health"]["incidents"]["open"] == 1


def test_semantic_error_and_transport_failure_are_error() -> None:
    semantic = api_for(FakeHost(server_snapshot(error=TelemetryError("DOMAIN", "Host telemetry unavailable")))).server()
    assert semantic["observation"]["state"] == "error"
    transport = api_for(FakeHost(failure=RuntimeError("private detail"))).server()
    assert transport["observation"]["state"] == "error"
    assert transport["error"] == "Host telemetry unavailable"
    assert "private detail" not in str(transport)


def test_unavailable_host_without_semantic_error_is_unavailable() -> None:
    payload = api_for(FakeHost(server_snapshot(available=False))).server()
    assert payload["available"] is False
    assert payload["observation"]["state"] == "unavailable"
    assert payload["identity"]["hostname"] is None
    assert payload["cpu"]["usage_percent"] is None


def test_optional_field_failure_does_not_invalidate_host_observation() -> None:
    snapshot = server_snapshot()
    snapshot = ServerHostSnapshot(
        host=snapshot.host,
        filesystems=(), filesystem_available=False, filesystem_error=TelemetryError("FS", "Filesystem detail unavailable"),
        interfaces=(), interface_detail_available=False, interface_detail_error=TelemetryError("NET", "Interface detail unavailable"),
        connection_states=(), connection_states_available=False, connection_states_error=TelemetryError("STATE", "Connection state unavailable"),
    )
    payload = api_for(FakeHost(snapshot)).server()
    assert payload["observation"]["state"] == "fresh"
    assert payload["available"] is True
    assert payload["disk"]["filesystem_detail"]["available"] is False
    assert payload["network"]["interface_detail"]["available"] is False


def test_health_and_incident_failures_are_isolated() -> None:
    payload = api_for(
        FakeHost(),
        health=FakeHealth({"available": False, "error": "Service health unavailable"}),
        incidents=FakeIncidents({"available": False, "error": "Incident summary unavailable"}),
    ).server()
    assert payload["observation"]["state"] == "fresh"
    assert payload["health"]["state"] == "unavailable"
    assert payload["health"]["incidents"]["available"] is False


def test_host_provider_bounds_filesystems_and_interfaces() -> None:
    class FakePsutil:
        def swap_memory(self): return SimpleNamespace(total=0, used=0, percent=0)
        def boot_time(self): return 0
        def net_if_addrs(self): return {"eth0": []}
        def net_connections(self, kind="inet"): return [SimpleNamespace(status="ESTABLISHED")]
        def disk_partitions(self, all=False): return [SimpleNamespace(mountpoint="/", fstype="ext4")] + [SimpleNamespace(mountpoint=f"/unsafe-{i}", fstype="x") for i in range(40)]
        def disk_usage(self, path): return SimpleNamespace(total=100, used=40, free=60, percent=40)
        def net_if_stats(self): return {f"eth{i}": SimpleNamespace(isup=True) for i in range(40)}
        def net_io_counters(self, pernic=True): return {f"eth{i}": SimpleNamespace(bytes_recv=i, bytes_sent=i + 1) for i in range(40)}

    system = SimpleNamespace(summary=lambda: SystemSummary(HostInfo("host", "Linux", "kernel", "x86_64", "3.12"), CpuInfo(1, 2, 10.0), MemoryInfo(4, 1, 3, 25), DiskInfo(20, 5, 15, 25)))
    service = HostTelemetryService(system_service=system, psutil_module=FakePsutil(), os_module=SimpleNamespace(getloadavg=lambda: (0.1, 0.2, 0.3)), time_module=SimpleNamespace(time=lambda: 100))
    result = service.server_snapshot()
    assert len(result.filesystems) <= service.MAX_FILESYSTEMS
    assert len(result.interfaces) <= service.MAX_INTERFACES
    assert all(item.mountpoint in service.SAFE_FILESYSTEM_MOUNTS for item in result.filesystems)
    assert all(len(item.name) <= 32 for item in result.interfaces)


def test_real_fastapi_server_route_get_only_and_overview_history_compatibility() -> None:
    class FakeDashboard:
        history_api = FakeHistory()
        def overview(self):
            return {"generated_at": NOW.isoformat(), "host": {"available": True}, "docker": {"containers": []}, "projects": {"projects": []}, "tunnel": {}, "handbook": []}

    app = create_app(
        dashboard_api=FakeDashboard(),
        incidents_api=FakeIncidents(),
        notifications_api=FakeNotifications(),
        service_health_api=FakeHealth(),
        server_api=api_for(FakeHost()),
    )
    client = TestClient(app)
    server = client.get("/api/server")
    assert server.status_code == 200
    assert server.json()["observation"]["state"] == "fresh"
    assert client.post("/api/server").status_code == 405
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/history/host?range=1h&limit=10").json()["points"][0]["range"] == "1h"


def test_server_payload_is_secret_safe() -> None:
    payload = api_for(FakeHost()).server()
    assert scan_payload(payload) == ()


def test_unexpected_exception_text_is_never_serialized() -> None:
    private_text = "PRIVATE_EXCEPTION_DETAILS_DO_NOT_LEAK"
    observation = Observation(
        transport_ok=False,
        available=False,
        state=ObservationState.ERROR,
        error=RuntimeError(private_text),
    )
    payload = ServerResponseMapper().to_response(
        server_snapshot(),
        observation,
        health=FakeHealth().payload,
        incidents=FakeIncidents().payload,
    )
    serialized = json.dumps(payload)
    assert private_text not in serialized
    assert payload["error"] is None
    assert payload["observation"]["error"] is None
    assert ServerResponseMapper._error(ObservationError("SAFE", "safe observation message")) == "safe observation message"
    assert ServerResponseMapper._error(TelemetryError("SAFE", "safe telemetry message")) == "safe telemetry message"
