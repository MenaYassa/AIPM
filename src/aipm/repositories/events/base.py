from __future__ import annotations

from typing import Protocol, Sequence

from aipm.models.events import Event, EventFilter
from aipm.models.health_observation import HealthObservation


class EventRepository(Protocol):
    def initialize(self) -> None: ...

    def save_processed_run(
        self,
        source_run_id: int,
        processed_at,
        observations: Sequence[HealthObservation],
        events: Sequence[Event],
    ) -> bool: ...

    def get_previous_health_observation(self, project_path: str, before_run_id: int) -> HealthObservation | None: ...

    def get_events(self, event_filter: EventFilter) -> list[Event]: ...

    def get_events_page(self, event_filter: EventFilter, *, after: tuple[object, int] | None = None) -> list[Event]: ...

    def get_event(self, event_id: int) -> Event | None: ...

    def get_events_by_ids(self, event_ids: tuple[int, ...]) -> list[Event]: ...

    def get_event_by_key(self, event_key: str) -> Event | None: ...

    def is_processed(self, source_run_id: int) -> bool: ...

    def delete_old_events(self, cutoff) -> int: ...
