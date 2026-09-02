from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from aipm.models.mission_control_evidence import HistoricalPoint
from aipm.models.history import (
    ContainerHistoryPoint,
    HistoricalRun,
    HostHistoryPoint,
    ProjectHistoryPoint,
    SampleRunRecord,
    TunnelHistoryPoint,
)


class HistoryRepository(Protocol):
    def initialize(self) -> None: ...

    def save_sample(
        self,
        run: SampleRunRecord,
        host: HostHistoryPoint | None,
        containers: Sequence[ContainerHistoryPoint],
        projects: Sequence[ProjectHistoryPoint],
        tunnel: TunnelHistoryPoint | None,
    ) -> int: ...

    def save_resource_sample(self, sampled_at: datetime, containers: Sequence[ContainerHistoryPoint], *, duration_ms: int | None, status: str, error_code: str | None = None) -> int: ...

    def get_latest_resource_samples(self) -> list[ContainerHistoryPoint]: ...

    def get_latest_project_samples(self) -> list[ProjectHistoryPoint]: ...

    def get_resource_history(self, name: str | None, start: datetime | None, end: datetime | None, limit: int) -> list[ContainerHistoryPoint]: ...

    def get_runs(self, after_id: int | None, limit: int) -> list[HistoricalRun]: ...

    def get_run(self, run_id: int) -> HistoricalRun | None: ...

    def get_previous_run(self, run_id: int) -> HistoricalRun | None: ...

    def get_host_history(self, start: datetime | None, end: datetime | None, limit: int) -> list[HostHistoryPoint]: ...

    def get_latest_host_at(self, end: datetime) -> HistoricalPoint | None: ...

    def get_latest_container_at(self, name: str, end: datetime) -> HistoricalPoint | None: ...

    def get_latest_project_at(self, name: str, end: datetime) -> HistoricalPoint | None: ...

    def get_latest_tunnel_at(self, end: datetime) -> HistoricalPoint | None: ...

    def get_containers_for_run(self, run_id: int) -> list[ContainerHistoryPoint]: ...

    def get_projects_for_run(self, run_id: int) -> list[ProjectHistoryPoint]: ...

    def get_tunnel_for_run(self, run_id: int) -> TunnelHistoryPoint | None: ...

    def get_container_history(
        self,
        name: str | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ) -> list[ContainerHistoryPoint]: ...

    def get_project_history(
        self,
        name: str | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ) -> list[ProjectHistoryPoint]: ...

    def get_tunnel_history(self, start: datetime | None, end: datetime | None, limit: int) -> list[TunnelHistoryPoint]: ...

    def delete_older_than(self, cutoff: datetime) -> int: ...

    def close(self) -> None: ...
