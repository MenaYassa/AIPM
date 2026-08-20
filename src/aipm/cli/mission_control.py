from __future__ import annotations

import typer

from aipm.capabilities.dashboard.context import MissionControlContext
from aipm.core.app import Application
from aipm.tui.session import TuiSession, VIEWS


tui_app = typer.Typer(
    help="Observation-only Mission Control terminal interface.",
    no_args_is_help=False,
)


@tui_app.callback(invoke_without_command=True)
def tui(
    view: str = typer.Option(
        "",
        "--view",
        help="View to render once. Omit for the terminal navigation menu.",
    ),
    watch: bool = typer.Option(False, "--watch", help="Refresh one bounded view at a fixed interval."),
    interval: int = typer.Option(60, "--interval", min=1, max=3600, help="Seconds between bounded refreshes."),
    iterations: int = typer.Option(1, "--iterations", min=1, max=60, help="Maximum refresh count in watch mode."),
    incident_id: int | None = typer.Option(None, "--incident-id", min=1, help="Incident ID for the timeline view."),
) -> None:
    """Render safe Mission Control observations without lifecycle or action controls."""

    selected = view.strip().lower() or None
    if selected is not None and selected not in VIEWS:
        typer.echo(f"Unknown view: {selected}", err=True)
        raise typer.Exit(code=2)
    context = MissionControlContext.from_application(Application.create())
    session = TuiSession(context)
    session.incident_id = incident_id
    exit_code = session.run(
        view=selected,
        watch=watch,
        interval_seconds=interval,
        iterations=iterations,
    )
    if exit_code:
        raise typer.Exit(code=exit_code)
