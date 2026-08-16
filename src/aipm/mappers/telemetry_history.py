from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
from typing import Any

from aipm.models.history import (
    ContainerHistoryPoint,
    HistoricalSample,
    HistoryResponse,
    HostHistoryPoint,
    ProjectHistoryPoint,
    SampleRunRecord,
    TunnelHistoryPoint,
)
from aipm.models.telemetry import DashboardSnapshot


class TelemetryHistoryMapper:
    """Map a typed DashboardSnapshot into normalized historical measurements."""

    def to_sample(self, snapshot: DashboardSnapshot, *, duration_ms: int | None = None) -> HistoricalSample:
        sampled_at = _utc(snapshot.generated_at)
        return HistoricalSample(
            run=SampleRunRecord(
                sampled_at=sampled_at,
                host_available=snapshot.host.available,
                docker_available=snapshot.docker.available,
                projects_available=snapshot.projects.available,
                tunnel_state=snapshot.tunnel.state,
                duration_ms=duration_ms,
            ),
            host=self._host(snapshot, sampled_at),
            containers=tuple(self._container(item, sampled_at) for item in snapshot.docker.containers),
            projects=tuple(
                ProjectHistoryPoint(
                    sampled_at=sampled_at,
                    name=item.project.name,
                    path=item.project.path,
                    branch=item.project.git.branch if item.project.git else None,
                    has_git=item.project.capabilities.has_git,
                    has_compose=item.project.capabilities.has_compose,
                    dirty=item.project.git.dirty if item.project.git else None,
                    ahead=item.project.git.ahead if item.project.git else None,
                    behind=item.project.git.behind if item.project.git else None,
                )
                for item in snapshot.projects.projects
            ),
            tunnel=TunnelHistoryPoint(
                sampled_at=sampled_at,
                state=snapshot.tunnel.state,
                source=snapshot.tunnel.source,
                systemd=snapshot.tunnel.systemd,
                local_containers=snapshot.tunnel.local_containers,
            ),
        )

    @staticmethod
    def _host(snapshot: DashboardSnapshot, sampled_at: datetime) -> HostHistoryPoint:
        system = snapshot.host.system
        cpu = system.cpu if system else None
        memory = system.memory if system else None
        disk = system.disk if system else None
        host = system.host if system else None
        return HostHistoryPoint(
            sampled_at=sampled_at,
            hostname=host.hostname if host else None,
            cpu_percent=cpu.usage_percent if cpu else None,
            load_one=snapshot.host.load_one,
            load_five=snapshot.host.load_five,
            load_fifteen=snapshot.host.load_fifteen,
            memory_total_gb=memory.total_gb if memory else None,
            memory_used_gb=memory.used_gb if memory else None,
            memory_available_gb=memory.available_gb if memory else None,
            memory_percent=memory.percent if memory else None,
            swap_total_gb=snapshot.host.swap.total_gb if snapshot.host.swap.available else None,
            swap_used_gb=snapshot.host.swap.used_gb if snapshot.host.swap.available else None,
            swap_percent=snapshot.host.swap.percent if snapshot.host.swap.available else None,
            disk_total_gb=disk.total_gb if disk else None,
            disk_used_gb=disk.used_gb if disk else None,
            disk_free_gb=disk.free_gb if disk else None,
            disk_percent=disk.percent if disk else None,
            network_interfaces=snapshot.host.network.interfaces if snapshot.host.network.available else None,
            network_established=snapshot.host.network.established if snapshot.host.network.available else None,
            available=snapshot.host.available,
        )

    @staticmethod
    def _container(item, sampled_at: datetime) -> ContainerHistoryPoint:
        freshness = item.resources.freshness
        resource_sampled_at = freshness.sampled_at if freshness else (sampled_at if item.resources.available else None)
        resource_status = freshness.status.value if freshness else ("fresh" if item.resources.available else "unavailable")
        resource_age_seconds = freshness.age_seconds if freshness else (0 if item.resources.available else None)
        return ContainerHistoryPoint(
            sampled_at=sampled_at,
            container_id=item.container.id,
            container_name=item.container.name,
            image=item.container.image,
            state=item.container.state,
            health=item.container.health,
            stack=item.container.stack,
            restart_count=item.restart_count,
            cpu_percent=item.resources.cpu_percent,
            memory_used_mb=item.resources.memory_used_mb,
            memory_limit_mb=item.resources.memory_limit_mb,
            memory_percent=item.resources.memory_percent,
            stats_available=item.resources.available,
            resource_sampled_at=resource_sampled_at,
            resource_status=resource_status,
            resource_age_seconds=resource_age_seconds,
        )


class HistoryResponseMapper:
    """Map typed historical query responses into safe JSON-ready structures."""

    def to_response(self, response: HistoryResponse) -> dict[str, Any]:
        return {
            "available": response.available,
            "status": response.status,
            "error": response.error,
            "points": [self._point(point) for point in response.points],
        }

    @staticmethod
    def _point(point: object) -> dict[str, Any]:
        data = {}
        for field in fields(point):
            key = field.name
            value = getattr(point, key)
            if isinstance(value, datetime):
                data[key] = _utc(value).isoformat()
            elif isinstance(value, tuple):
                data[key] = list(value)
            else:
                data[key] = value
        return data


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Dashboard snapshot timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc)
