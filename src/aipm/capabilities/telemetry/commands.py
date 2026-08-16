from __future__ import annotations

from rich import print

from aipm.capabilities.dashboard.api import DashboardApi
from aipm.core.app import Application
from aipm.mappers.telemetry_history import TelemetryHistoryMapper
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository
from aipm.services.telemetry.runner import TelemetryRunner
from aipm.services.telemetry.sampler import TelemetrySampler


def build_sampler(application: Application) -> tuple[TelemetrySampler, SQLiteHistoryRepository] | None:
    config = application.config.telemetry
    if not config.enabled:
        return None
    dashboard_api = DashboardApi.from_application(application)
    repository = SQLiteHistoryRepository(config.database_path)
    sampler = TelemetrySampler(
        telemetry_service=dashboard_api.telemetry,
        mapper=TelemetryHistoryMapper(),
        repository=repository,
        config=config,
        logger=application.logger,
    )
    return sampler, repository


def sample() -> None:
    """Collect and persist exactly one read-only telemetry sample."""
    application = Application.create()
    built = build_sampler(application)
    if built is None:
        print("[yellow]Telemetry sampling is disabled by configuration.[/yellow]")
        return
    sampler, repository = built
    try:
        result = sampler.sample_once()
    finally:
        repository.close()
    if result.error:
        print(f"[yellow]Telemetry sample unavailable:[/yellow] {result.error}")
        return
    if result.skipped:
        print("[yellow]Telemetry sampling skipped.[/yellow]")
        return
    print(
        f"[green]Telemetry sample stored.[/green] run={result.run_id} "
        f"host={result.host_rows} containers={result.container_rows} "
        f"projects={result.project_rows} retention_deleted={result.retention_deleted}"
    )


def run() -> None:
    """Run one persistent, systemd-managed read-only telemetry sampler."""
    application = Application.create()
    built = build_sampler(application)
    if built is None:
        print("[yellow]Telemetry runner is disabled by configuration.[/yellow]")
        return
    sampler, repository = built
    runner = TelemetryRunner(sampler, application.config.telemetry, logger=application.logger)
    try:
        runner.run()
    finally:
        repository.close()
