from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aipm.capabilities.dashboard.api import DashboardApi
from aipm.capabilities.dashboard.incidents_api import DashboardIncidentsApi
from aipm.capabilities.dashboard.notifications_api import DashboardNotificationsApi
from aipm.capabilities.dashboard.service_health_api import DashboardServiceHealthApi
from aipm.capabilities.dashboard.server_api import DashboardServerApi
from aipm.capabilities.dashboard.docker_api import DashboardDockerApi
from aipm.capabilities.dashboard.project_api import DashboardProjectApi
from aipm.capabilities.dashboard.systemd_api import DashboardSystemdApi
from aipm.capabilities.dashboard.logs_api import DashboardLogsApi
from aipm.capabilities.dashboard.settings_api import DashboardSettingsApi
from aipm.capabilities.dashboard.context import MissionControlContext
from aipm.core.app import Application

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    dashboard_api: DashboardApi | None = None,
    application: Application | None = None,
    incidents_api: DashboardIncidentsApi | None = None,
    notifications_api: DashboardNotificationsApi | None = None,
    service_health_api: DashboardServiceHealthApi | None = None,
    server_api: DashboardServerApi | None = None,
    docker_api: DashboardDockerApi | None = None,
    project_api: DashboardProjectApi | None = None,
    systemd_api: DashboardSystemdApi | None = None,
    logs_api: DashboardLogsApi | None = None,
    settings_api: DashboardSettingsApi | None = None,
) -> FastAPI:
    """Create the HTTP adapter without owning infrastructure business logic."""
    app_context = application or Application.create()
    context = MissionControlContext.from_application(
        app_context,
        dashboard=dashboard_api,
        incidents=incidents_api,
        notifications=notifications_api,
        service_health=service_health_api,
        server=server_api,
        docker=docker_api,
        projects=project_api,
        systemd=systemd_api,
        logs=logs_api,
        settings=settings_api,
    )
    api = context.dashboard
    event_api = context.incidents
    notification_api = context.notifications
    health_api = context.service_health
    host_api = context.server
    docker_detail_api = context.docker
    project_detail_api = context.projects
    systemd_observation_api = context.systemd
    logs_observation_api = context.logs
    settings_posture_api = context.settings
    app = FastAPI(title="AIPM Mission Control", version="0.1.0", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/overview")
    def overview():
        return api.overview()

    @app.get("/api/server")
    def server():
        return host_api.server()

    @app.get("/api/docker/summary")
    def docker_summary(limit: int = 200, project: str | None = None):
        return docker_detail_api.summary(limit=limit, project=project)

    @app.get("/api/docker/containers")
    def docker_containers(limit: int = 200, project: str | None = None):
        return docker_detail_api.containers(limit=limit, project=project)

    @app.get("/api/docker/containers/{container_id}")
    def docker_container(container_id: str):
        return docker_detail_api.container(container_id)

    @app.get("/api/docker/images")
    def docker_images(limit: int = 200):
        return docker_detail_api.images(limit=limit)

    @app.get("/api/docker/volumes")
    def docker_volumes(limit: int = 200):
        return docker_detail_api.volumes(limit=limit)

    @app.get("/api/docker/networks")
    def docker_networks(limit: int = 200):
        return docker_detail_api.networks(limit=limit)

    @app.get("/api/projects")
    def projects(limit: int = 200, search: str | None = None, status: str | None = None, scope: str = "all"):
        return project_detail_api.projects(limit=limit, search=search, status=status, scope=scope)

    @app.get("/api/projects/{project_id}")
    def project_detail(project_id: str):
        return project_detail_api.project(project_id)

    @app.get("/api/projects/{project_id}/containers")
    def project_containers(project_id: str):
        return project_detail_api.containers(project_id)

    @app.get("/api/projects/{project_id}/health")
    def project_health(project_id: str):
        return project_detail_api.health(project_id)

    @app.get("/api/systemd/units")
    def systemd_units(limit: int = Query(20, ge=1, le=20)):
        return systemd_observation_api.units(limit=limit)

    @app.get("/api/systemd/units/{unit_id}")
    def systemd_unit(unit_id: str):
        return systemd_observation_api.unit(unit_id)

    @app.get("/api/logs")
    def logs(
        source: str = "aipm-dashboard",
        since: str | None = None,
        until: str | None = None,
        severity: str | None = None,
        unit: str | None = None,
        project: str | None = None,
        limit: int = 200,
        max_bytes: int = 100_000,
        cursor: str | None = None,
    ):
        return logs_observation_api.logs(
            source=source,
            since=since,
            until=until,
            severity=severity,
            unit=unit,
            project=project,
            limit=limit,
            max_bytes=max_bytes,
            cursor=cursor,
        )

    @app.get("/api/history/host")
    def history_host(range_name: str = Query("24h", alias="range"), limit: int = 500):
        return _history_response(api, "host", range_name, limit)

    @app.get("/api/history/containers")
    def history_containers(name: str | None = None, range_name: str = Query("24h", alias="range"), limit: int = 500):
        return _history_response(api, "containers", range_name, limit, name=name)

    @app.get("/api/history/container-resources")
    def history_container_resources(name: str | None = None, range_name: str = Query("24h", alias="range"), limit: int = 500):
        return _history_response(api, "resources", range_name, limit, name=name)

    @app.get("/api/history/projects")
    def history_projects(name: str | None = None, range_name: str = Query("24h", alias="range"), limit: int = 500):
        return _history_response(api, "projects", range_name, limit, name=name)

    @app.get("/api/history/tunnel")
    def history_tunnel(range_name: str = Query("24h", alias="range"), limit: int = 500):
        return _history_response(api, "tunnel", range_name, limit)

    @app.get("/api/history/compare")
    def history_compare(resource_type: str = "host", range_name: str = Query("24h", alias="range"), name: str | None = None, baseline: str | None = None, current: str | None = None):
        if api.history_api is None:
            return {"available": False, "status": "unavailable", "error": "Historical telemetry unavailable", "resource_type": resource_type, "resource_id": name, "baseline": {}, "current": {}, "changes": [], "links": []}
        return api.history_api.compare(resource_type=resource_type, range_name=range_name, name=name, baseline=baseline, current=current)

    @app.get("/api/events")
    def events(
        range_name: str = Query("24h", alias="range"),
        severity: str | None = None,
        event_type: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        limit: int = 500,
        cursor: str | None = None,
    ):
        return event_api.events_page(range_name=range_name, severity=severity, event_type=event_type, resource_type=resource_type, resource_id=resource_id, limit=limit, cursor=cursor)

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
        cursor: str | None = None,
    ):
        return event_api.incidents_page(range_name=range_name, status=status, severity=severity, resource_id=resource_id, limit=limit, cursor=cursor)

    @app.get("/api/incidents/{incident_id}")
    def incident_detail(incident_id: int):
        return event_api.incident(incident_id)

    @app.get("/api/incidents/{incident_id}/timeline")
    def incident_timeline(incident_id: int, limit: int = 200, cursor: str | None = None):
        return event_api.timeline(incident_id, limit=limit, cursor=cursor)

    @app.get("/api/settings/posture")
    def settings_posture():
        return settings_posture_api.posture()

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

    @app.get("/api/notification-metrics")
    def notification_metrics():
        return notification_api.metrics()

    @app.get("/api/services")
    def services():
        return health_api.services()

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
    if kind == "resources":
        return history_api.resources(name=name, range_name=range_name, limit=limit)
    if kind == "projects":
        return history_api.projects(name=name, range_name=range_name, limit=limit)
    return history_api.tunnel(range_name, limit)


def run(host: str = "127.0.0.1", port: int = 8787, reload: bool = False) -> None:
    import uvicorn

    uvicorn.run("aipm.dashboard.server:create_app", host=host, port=port, reload=reload, factory=True)
