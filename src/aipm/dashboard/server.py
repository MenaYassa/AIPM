from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aipm.capabilities.dashboard.api import DashboardApi
from aipm.core.app import Application

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    dashboard_api: DashboardApi | None = None,
    application: Application | None = None,
) -> FastAPI:
    """Create the HTTP adapter without owning infrastructure business logic."""
    api = dashboard_api or DashboardApi.from_application(application or Application.create(), include_history=True)
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
