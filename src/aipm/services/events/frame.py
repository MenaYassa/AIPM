from __future__ import annotations

from dataclasses import dataclass

from aipm.models.history import (
    ContainerHistoryPoint,
    HistoricalRun,
    ProjectHistoryPoint,
    TunnelHistoryPoint,
)
from aipm.repositories.telemetry.base import HistoryRepository


@dataclass(slots=True, frozen=True)
class HistoricalFrame:
    current: HistoricalRun
    previous: HistoricalRun | None
    current_containers: tuple[ContainerHistoryPoint, ...]
    previous_containers: tuple[ContainerHistoryPoint, ...]
    current_projects: tuple[ProjectHistoryPoint, ...]
    previous_projects: tuple[ProjectHistoryPoint, ...]
    current_tunnel: TunnelHistoryPoint | None
    previous_tunnel: TunnelHistoryPoint | None


class HistoricalFrameService:
    """Reconstruct adjacent typed facts from the existing history repository."""

    def __init__(self, repository: HistoryRepository) -> None:
        self.repository = repository

    def for_run(self, run_id: int) -> HistoricalFrame | None:
        current = self.repository.get_run(run_id)
        if current is None:
            return None
        previous = self.repository.get_previous_run(run_id)
        return HistoricalFrame(
            current=current,
            previous=previous,
            current_containers=tuple(self.repository.get_containers_for_run(run_id)),
            previous_containers=tuple(self.repository.get_containers_for_run(previous.id)) if previous else (),
            current_projects=tuple(self.repository.get_projects_for_run(run_id)),
            previous_projects=tuple(self.repository.get_projects_for_run(previous.id)) if previous else (),
            current_tunnel=self.repository.get_tunnel_for_run(run_id),
            previous_tunnel=self.repository.get_tunnel_for_run(previous.id) if previous else None,
        )
