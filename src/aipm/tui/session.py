from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from rich.console import Console

from aipm.capabilities.dashboard.context import MissionControlContext
from aipm.tui.renderers import TuiProjection, project_view, render_projection


MAX_PAGE_TRAVERSAL = 10

VIEWS = (
    "overview",
    "server",
    "docker",
    "projects",
    "systemd",
    "logs",
    "events",
    "incidents",
    "timeline",
    "history",
    "settings",
)


class TuiSession:
    """Observation-only terminal navigation and refresh session."""

    def __init__(
        self,
        context: MissionControlContext,
        *,
        console: Console | None = None,
        input_fn: Callable[[str], str] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.context = context
        self.console = console or Console()
        self.input_fn = input_fn or self.console.input
        self.sleep_fn = sleep_fn
        self.current_view = "overview"
        self.incident_id: int | None = None
        self.last_projection: TuiProjection | None = None
        self.current_cursor: str | None = None
        self.page_index = 0

    def render_view(self, view: str | None = None) -> TuiProjection:
        selected = view or self.current_view
        if selected not in VIEWS:
            projection = TuiProjection(
                title="Mission Control",
                status="error",
                available=False,
                lines=(("Message", "Unknown observation view"),),
            )
            render_projection(self.console, projection)
            self.last_projection = projection
            return projection
        if selected != self.current_view:
            self._reset_pagination()
        self.current_view = selected
        response = self._observe(selected, cursor=self.current_cursor)
        projection = project_view(selected, response)
        render_projection(self.console, projection)
        self.last_projection = projection
        return projection

    def _reset_pagination(self) -> None:
        self.current_cursor = None
        self.page_index = 0
        self.last_projection = None

    def _next_page(self) -> bool:
        projection = self.last_projection
        if projection is None or not projection.has_more:
            self._print_error("No next bounded page is available.")
            return False
        if not projection.next_cursor:
            self._print_error("The next page cursor is unavailable.")
            return False
        if self.page_index >= MAX_PAGE_TRAVERSAL - 1:
            self._print_error("Maximum bounded page traversal reached.")
            return False
        self.current_cursor = projection.next_cursor
        self.page_index += 1
        self.render_view()
        return True

    def run(
        self,
        *,
        view: str | None = None,
        interactive: bool | None = None,
        watch: bool = False,
        interval_seconds: int = 60,
        iterations: int = 1,
    ) -> int:
        if interval_seconds < 1 or interval_seconds > 3600:
            return 2
        if iterations < 1 or iterations > 60:
            return 2
        if view is not None and view not in VIEWS:
            self._print_error(f"Unknown view: {view}")
            return 2
        if watch:
            try:
                for index in range(iterations):
                    if index:
                        self.sleep_fn(interval_seconds)
                    self.render_view(view)
            except KeyboardInterrupt:
                return 0
            return 0
        self.render_view(view)
        if interactive is None:
            interactive = bool(self.console.is_terminal and view is None)
        if not interactive:
            return 0
        return self._interactive_loop()

    def _interactive_loop(self) -> int:
        self.console.print("[dim]Choose a view number, r to refresh, b to return to overview, q to quit.[/dim]")
        while True:
            try:
                choice = self.input_fn(self._prompt()).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return 0
            if choice in {"q", "quit", "exit"}:
                return 0
            if choice in {"r", "refresh"}:
                self.render_view()
                continue
            if choice in {"n", "next"}:
                self._next_page()
                continue
            if choice in {"b", "back"}:
                self._reset_pagination()
                self.render_view("overview")
                continue
            if choice.isdigit() and 1 <= int(choice) <= len(VIEWS):
                self.render_view(VIEWS[int(choice) - 1])
                continue
            self._print_error("Choose a listed view, r, b, or q.")

    def _prompt(self) -> str:
        menu = " ".join(f"{index}:{name}" for index, name in enumerate(VIEWS, start=1))
        page = " n:next" if self.last_projection and self.last_projection.has_more else ""
        return f"[{menu}{page} r:refresh b:back q:quit] > "

    def _print_error(self, message: str) -> None:
        self.console.print(f"[yellow]TUI input:[/yellow] {message[:256]}")

    def _observe(self, view: str, *, cursor: str | None = None) -> dict[str, Any]:
        try:
            if view == "overview":
                return self.context.dashboard.overview()
            if view == "server":
                return self.context.server.server()
            if view == "docker":
                return self.context.docker.summary(limit=20)
            if view == "projects":
                return self.context.projects.projects(limit=20)
            if view == "systemd":
                return self.context.systemd.units(limit=20)
            if view == "logs":
                return self.context.logs.logs(limit=50, max_bytes=20_000, cursor=cursor)
            if view == "events":
                return self.context.incidents.events_page(range_name="24h", limit=20, cursor=cursor)
            if view == "incidents":
                return self.context.incidents.incidents_page(range_name="7d", limit=20, cursor=cursor)
            if view == "timeline":
                if self.incident_id is None:
                    return {"available": False, "status": "not_observed", "error": "Select an incident ID before opening a timeline"}
                return self.context.incidents.timeline(self.incident_id, limit=50, cursor=cursor)
            if view == "history":
                history = self.context.history
                if history is None:
                    return {"available": False, "status": "unavailable", "error": "Historical telemetry unavailable"}
                return history.host("24h", 50)
            if view == "settings":
                return self.context.settings.posture()
        except Exception:
            return {"available": False, "status": "unavailable", "error": "Observation unavailable"}
        return {"available": False, "status": "error", "error": "Unknown observation view"}
