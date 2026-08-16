from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from aipm.models.history import (
    ContainerHistoryPoint,
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

    def get_host_history(self, start: datetime | None, end: datetime | None, limit: int) -> list[HostHistoryPoint]: ...

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
