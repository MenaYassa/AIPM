from __future__ import annotations

from rich import print

from aipm.engines.health.engine import HealthEngine
from aipm.core.app import Application
from aipm.repositories.events.sqlite import SQLiteEventRepository
from aipm.repositories.incidents.sqlite import SQLiteIncidentRepository
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository
from aipm.services.events.derivation import EventDerivationService
from aipm.services.events.frame import HistoricalFrameService
from aipm.services.events.health import HealthEvidenceService
from aipm.services.events.processor import EventProcessor
from aipm.services.events.runner import EventRunner
from aipm.services.incidents.engine import IncidentEngine
from aipm.services.project.service import ProjectService


def build_processor(application: Application):
    if not application.config.events.enabled:
        return None
    database_path = application.config.telemetry.database_path
    history = SQLiteHistoryRepository(database_path)
    events = SQLiteEventRepository(database_path)
    incidents = SQLiteIncidentRepository(database_path)
    # The read-only dashboard consumes the notification projection from this
    # shared database, so provision its schema here alongside events/incidents.
    SQLiteNotificationRepository(database_path).initialize()
    processor = EventProcessor(
        frame_service=HistoricalFrameService(history),
        event_repository=events,
        derivation=EventDerivationService(),
        health_evidence=HealthEvidenceService(ProjectService(application), HealthEngine()),
        incident_engine=IncidentEngine(incidents),
        logger=application.logger,
    )
    return processor, history, events, incidents


def process(run_id: int | None = None) -> None:
    application = Application.create()
    built = build_processor(application)
    if built is None:
        print("[yellow]Event processing is disabled by configuration.[/yellow]")
        return
    processor, history, _events, _incidents = built
    runs = [history.get_run(run_id)] if run_id is not None else history.get_runs(None, 1000)
    results = []
    for run in runs:
        if run is not None:
            results.append(processor.process_run(run.id))
    processed = sum(item.processed for item in results)
    event_count = sum(item.event_count for item in results)
    incident_count = sum(item.incident_count for item in results)
    errors = sum(item.error is not None for item in results)
    print(f"[green]Event processing complete.[/green] runs={len(results)} processed={processed} events={event_count} incidents={incident_count} errors={errors}")


def run() -> None:
    application = Application.create()
    built = build_processor(application)
    if built is None:
        print("[yellow]Event runner is disabled by configuration.[/yellow]")
        return
    processor, history, events, incidents = built
    runner = EventRunner(processor, history, events, application.config.events, logger=application.logger)
    runner.run()
