from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from aipm.models.events import EventProcessResult
from aipm.repositories.events.base import EventRepository
from aipm.services.events.derivation import EventDerivationService
from aipm.services.events.frame import HistoricalFrameService
from aipm.services.events.health import HealthEvidenceService
from aipm.services.incidents.engine import IncidentEngine


class EventProcessor:
    """Process committed telemetry runs into idempotent events and incidents."""

    def __init__(
        self,
        frame_service: HistoricalFrameService,
        event_repository: EventRepository,
        derivation: EventDerivationService,
        health_evidence: HealthEvidenceService,
        incident_engine: IncidentEngine,
        *,
        clock: Callable[[], datetime] | None = None,
        logger: Any | None = None,
    ) -> None:
        self.frame_service = frame_service
        self.event_repository = event_repository
        self.derivation = derivation
        self.health_evidence = health_evidence
        self.incident_engine = incident_engine
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.logger = logger

    def process_run(self, source_run_id: int) -> EventProcessResult:
        try:
            if self.event_repository.is_processed(source_run_id):
                return EventProcessResult(source_run_id, False, 0, 0)
            frame = self.frame_service.for_run(source_run_id)
            if frame is None:
                return EventProcessResult(source_run_id, False, 0, 0, "Telemetry run not found")
            observations = self.health_evidence.observe(source_run_id, frame.current.sampled_at, frame.current_projects)
            previous = lambda path: self.event_repository.get_previous_health_observation(path, source_run_id)
            health_events = self.health_evidence.transition_events(observations, previous)
            events = self.derivation.derive(frame, health_events)
            committed = self.event_repository.save_processed_run(source_run_id, self.clock(), observations, events)
            if not committed:
                return EventProcessResult(source_run_id, False, 0, 0)
            persisted_events = tuple(
                persisted for event in events if (persisted := self.event_repository.get_event_by_key(event.event_key)) is not None
            )
            incident_count = self.incident_engine.apply(persisted_events)
            return EventProcessResult(source_run_id, True, len(persisted_events), incident_count)
        except Exception as exc:
            if self.logger is not None:
                self.logger.exception("Event processing failed", exc_info=exc)
            return EventProcessResult(source_run_id, False, 0, 0, "Event processing unavailable")
