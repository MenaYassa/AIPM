from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aipm.capabilities.dashboard.api import DashboardApi
from aipm.capabilities.dashboard.incidents_api import DashboardIncidentsApi
from aipm.capabilities.dashboard.notifications_api import DashboardNotificationsApi
from aipm.core.app import Application

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    dashboard_api: DashboardApi | None = None,
    application: Application | None = None,
    incidents_api: DashboardIncidentsApi | None = None,
    notifications_api: DashboardNotificationsApi | None = None,
) -> FastAPI:
    """Create the HTTP adapter without owning infrastructure business logic."""
    app_context = application or Application.create()
    api = dashboard_api or DashboardApi.from_application(app_context, include_history=True)
    event_api = incidents_api or DashboardIncidentsApi.from_application(app_context)
    notification_api = notifications_api or DashboardNotificationsApi.from_application(app_context)
    app = FastAPI(title="AIPM Mission Control", version="0.1.0", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/overview")
    def overview():
        return api.overview()

    @app.get("/api/history/host")
    def history_host(range_name: str = Query("24h", alias="range"), limit: int = 500):
        return _history_response(api, "host", range_name, limit)

    @app.get("/api/history/containers")
    def history_containers(name: str | None = None, range_name: str = Query("24h", alias="range"), limit: int = 500):
        return _history_response(api, "containers", range_name, limit, name=name)

    @app.get("/api/history/projects")
    def history_projects(name: str | None = None, range_name: str = Query("24h", alias="range"), limit: int = 500):
        return _history_response(api, "projects", range_name, limit, name=name)

    @app.get("/api/history/tunnel")
    def history_tunnel(range_name: str = Query("24h", alias="range"), limit: int = 500):
        return _history_response(api, "tunnel", range_name, limit)

    @app.get("/api/events")
    def events(
        range_name: str = Query("24h", alias="range"),
        severity: str | None = None,
        event_type: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        limit: int = 500,
    ):
        return event_api.events(range_name=range_name, severity=severity, event_type=event_type, resource_type=resource_type, resource_id=resource_id, limit=limit)

    @app.get("/api/events/{event_id}")
    def event_detail(event_id: int):
        return event_api.event(event_id)

    @app.get("/api/incidents")
    def incidents(
        range_name: str = Query("7d", alias="range"),
        status: str | None = None,
        severity: str | None = None,
        resource_id: str | None = None,
        limit: int = 500,
    ):
        return event_api.incidents(range_name=range_name, status=status, severity=severity, resource_id=resource_id, limit=limit)

    @app.get("/api/incidents/{incident_id}")
    def incident_detail(incident_id: int):
        return event_api.incident(incident_id)

    @app.post("/api/incidents/{incident_id}/acknowledge")
    def acknowledge_incident(incident_id: int):
        return event_api.acknowledge(incident_id)

    @app.get("/api/notifications")
    def notifications(status: str | None = None, incident_id: int | None = None, channel_id: str | None = None, include_suppressed: bool = False, limit: int = 100):
        return notification_api.notifications(status=status, incident_id=incident_id, channel_id=channel_id, include_suppressed=include_suppressed, limit=limit)

    @app.get("/api/notifications/{notification_id}")
    def notification_detail(notification_id: int):
        return notification_api.notification(notification_id)

    @app.get("/api/notification-channels")
    def notification_channels():
        return notification_api.channels()

    @app.get("/api/notification-policies")
    def notification_policies():
        return notification_api.policies()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _history_response(api: DashboardApi, kind: str, range_name: str, limit: int, name: str | None = None):
    history_api = getattr(api, "history_api", None)
    if history_api is None:
        return {"available": False, "status": "unavailable", "error": "Historical telemetry unavailable", "points": []}
    if kind == "host":
        return history_api.host(range_name, limit)
    if kind == "containers":
        return history_api.containers(name=name, range_name=range_name, limit=limit)
    if kind == "projects":
        return history_api.projects(name=name, range_name=range_name, limit=limit)
    return history_api.tunnel(range_name, limit)


def run(host: str = "127.0.0.1", port: int = 8787, reload: bool = False) -> None:
    import uvicorn

    uvicorn.run("aipm.dashboard.server:create_app", host=host, port=port, reload=reload, factory=True)
