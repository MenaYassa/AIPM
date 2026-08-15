from __future__ import annotations

import functools

import typer
from rich.console import Console

from aipm.core.app import Application
from aipm.core.exceptions import AIPMError


def cli_handler(action_name: str):
    """Log a CLI action and translate domain failures into CLI errors."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            app = Application.create()
            console = Console()
            params = [str(value) for value in args[1:]] + [f"{key}={value}" for key, value in kwargs.items()]
            app.logger.info("Executing '%s' with params: [%s]", action_name, ", ".join(params) or "None")
            try:
                return func(*args, **kwargs)
            except AIPMError as exc:
                app.logger.warning("Action '%s' failed: %s", action_name, exc)
                console.print(f"[red]Error:[/red] {exc}")
                raise typer.Exit(code=1) from exc
            except Exception as exc:
                app.logger.error("Critical error in '%s': %s", action_name, exc, exc_info=True)
                console.print(f"[bold red]Critical System Error:[/bold red] {exc}")
                console.print("[yellow]Check the configured AIPM log for details.[/yellow]")
                raise typer.Exit(code=1) from exc

        return wrapper

    return decorator