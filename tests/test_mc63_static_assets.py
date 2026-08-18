from fastapi.testclient import TestClient

from aipm.dashboard.server import create_app


class FakeHistory:
    def host(self, range_name, limit):
        return {"available": True, "points": [{"range": range_name, "limit": limit}]}

    def containers(self, name, range_name, limit):
        return {"available": True, "points": []}

    def projects(self, name, range_name, limit):
        return {"available": True, "points": []}

    def tunnel(self, range_name, limit):
        return {"available": True, "points": []}


class FakeDashboard:
    history_api = FakeHistory()

    def overview(self):
        return {"generated_at": "2026-08-18T00:00:00+00:00", "host": {"available": True}}


class FakeEvents:
    def events(self, **_kwargs):
        return {"available": True, "events": []}

    def event(self, _event_id):
        return {"available": False}

    def incidents(self, **_kwargs):
        return {"available": True, "incidents": []}

    def incident(self, _incident_id):
        return {"available": False}


class FakeNotifications:
    def notifications(self, **_kwargs):
        return {"available": True, "notifications": []}

    def notification(self, _notification_id):
        return {"available": False}

    def channels(self):
        return {"available": True, "channels": []}

    def policies(self):
        return {"available": True, "policies": []}

    def metrics(self):
        return {"available": True, "metrics": {}}


class FakeHealth:
    def services(self):
        return {"available": True, "overall": "healthy", "services": {}}


class FakeServer:
    def server(self):
        return {"available": True, "status": "ok", "observation": {"state": "fresh"}}


client = TestClient(
    create_app(
        dashboard_api=FakeDashboard(),
        incidents_api=FakeEvents(),
        notifications_api=FakeNotifications(),
        service_health_api=FakeHealth(),
        server_api=FakeServer(),
    )
)


def test_dashboard_uses_the_existing_static_mount_for_all_modules() -> None:
    html = client.get("/")
    assert html.status_code == 200
    for module in ("mission-control-state.js", "mission-control-scheduler.js", "mission-control-shell.js"):
        assert f"from '/static/{module}'" in html.text
        response = client.get(f"/static/{module}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")
        assert client.get(f"/{module}").status_code == 404


def test_static_mount_and_server_hash_route_are_read_only() -> None:
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/server").status_code == 200
    assert client.post("/api/server").status_code == 405
    assert "/static/mission-control-shell.js" in client.get("/").text
