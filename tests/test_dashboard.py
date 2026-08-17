from fastapi.testclient import TestClient

from aipm.dashboard.server import create_app


class FakeHistoryApi:
    def host(self, range_name, limit):
        return {"available": True, "status": "ok", "error": None, "points": [{"sampled_at": "2026-08-16T00:00:00+00:00", "cpu_percent": 10.0, "range": range_name, "limit": limit}]}

    def containers(self, name, range_name, limit):
        return {"available": True, "status": "ok", "error": None, "points": [{"container_name": name, "range": range_name, "limit": limit}]}

    def projects(self, name, range_name, limit):
        return {"available": True, "status": "ok", "error": None, "points": [{"name": name, "range": range_name, "limit": limit}]}

    def tunnel(self, range_name, limit):
        return {"available": True, "status": "ok", "error": None, "points": [{"state": "healthy", "range": range_name, "limit": limit}]}


class FakeServiceHealthApi:
    def services(self):
        return {"available": True, "status": "ok", "error": None, "overall": "healthy", "services": {"telemetry": {"state": "fresh"}, "mc3": {"state": "fresh"}}}


class FakeDashboardApi:
    history_api = FakeHistoryApi()

    def overview(self):
        return {
            "generated_at": "2026-08-16T00:00:00+00:00",
            "host": {
                "available": True,
                "status": "healthy",
                "hostname": "test-host",
                "os": "Linux",
                "kernel": "test",
                "architecture": "x86_64",
                "python": "3.12",
                "uptime": {"seconds": 60, "label": "0d 0h 1m"},
                "load": {"one": 0.1, "five": 0.1, "fifteen": 0.1},
                "cpu": {"usage_percent": 1.0, "physical_cores": 1, "logical_cores": 2},
                "memory": {"total_gb": 4.0, "used_gb": 1.0, "available_gb": 3.0, "percent": 25.0},
                "swap": {"available": True, "total_gb": 1.0, "used_gb": 0.0, "percent": 0.0},
                "disk": {"total_gb": 20.0, "used_gb": 5.0, "free_gb": 15.0, "percent": 25.0},
                "network": {"available": True, "interfaces": 1, "established": 0},
            },
            "docker": {"available": False, "status": "unknown", "containers": [], "summary": {"total": 0, "running": 0, "stopped": 0, "unhealthy": 0}},
            "tunnel": {"state": "unknown", "source": "not-detected", "local_containers": [], "systemd": None, "available": True},
            "projects": {"available": True, "status": "healthy", "search_paths": [], "projects": []},
            "handbook": [],
        }


client = TestClient(create_app(dashboard_api=FakeDashboardApi(), service_health_api=FakeServiceHealthApi()))


def test_dashboard_healthz():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_overview_shape():
    response = client.get("/api/overview")
    assert response.status_code == 200
    payload = response.json()
    assert {"generated_at", "host", "docker", "tunnel", "projects", "handbook"} <= payload.keys()
    assert {"cpu", "memory", "disk", "load", "uptime"} <= payload["host"].keys()
    assert isinstance(payload["docker"]["containers"], list)
    assert isinstance(payload["projects"]["projects"], list)


def test_dashboard_history_routes_preserve_safe_contract():
    host = client.get("/api/history/host?range=1h&limit=10")
    assert host.status_code == 200
    assert host.json()["points"][0]["range"] == "1h"
    assert host.json()["points"][0]["limit"] == 10

    containers = client.get("/api/history/containers?name=app&range=24h")
    assert containers.status_code == 200
    assert containers.json()["points"][0]["container_name"] == "app"


def test_dashboard_service_health_is_read_only_get_route():
    response = client.get("/api/services")
    assert response.status_code == 200
    assert response.json()["services"]["telemetry"]["state"] == "fresh"
    assert client.post("/api/services").status_code == 405


def test_dashboard_serves_read_only_mc5_interface():
    response = client.get("/")
    assert response.status_code == 200
    assert "AIPM Mission Control" in response.text
    assert "Service Pulse" in response.text
    assert "MC-3 Event Stream" in response.text
    assert "Notification Safety" in response.text
    assert "/api/services" in response.text
    assert "/api/events?range=24h&limit=50" in response.text
    assert "setInterval(loadServices,15000)" in response.text
    assert "acknowledge_incident" not in response.text
