"""Safe mappers for the additive read-only Docker detail contract."""

from __future__ import annotations

from typing import Any

from aipm.mappers.docker import DockerMapper
from aipm.models.container import Container
from aipm.models.docker import (
    DockerContainerDetail,
    DockerImageSummary,
    DockerNetworkSummary,
    DockerVolumeSummary,
)
from aipm.models.telemetry import ResourceStats


class DockerDetailMapper:
    """Convert Docker SDK objects into bounded, non-sensitive domain models."""

    @staticmethod
    def container(raw: Any, *, resources: ResourceStats | None = None) -> DockerContainerDetail:
        base = raw if isinstance(raw, Container) else DockerMapper.container(raw)
        attrs = getattr(raw, "attrs", {}) or {}
        state = attrs.get("State", {}) or {}
        labels = dict(base.labels or {})
        network_settings = attrs.get("NetworkSettings", {}) or {}
        networks = tuple(sorted(str(name)[:64] for name in (network_settings.get("Networks", {}) or {}).keys()))
        mounts = attrs.get("Mounts", []) or []
        mount_kinds = tuple(sorted({str(item.get("Type"))[:32] for item in mounts if item.get("Type")}))
        service_name = labels.get("com.docker.compose.service")
        return DockerContainerDetail(
            id=base.id,
            name=base.name,
            project_key=base.stack,
            service_name=str(service_name)[:128] if service_name else None,
            image=base.image,
            state=base.state,
            health=base.health,
            restart_count=int(state.get("RestartCount", 0) or 0),
            started_at=state.get("StartedAt"),
            ports=tuple(str(port)[:64] for port in base.ports[:32]),
            networks=networks[:32],
            mount_kinds=mount_kinds[:16],
            resources=resources,
        )

    @staticmethod
    def image(raw: Any) -> DockerImageSummary:
        attrs = getattr(raw, "attrs", {}) or {}
        short_id = str(getattr(raw, "short_id", "") or "").replace("sha256:", "")[:12]
        tags = tuple(str(tag)[:256] for tag in (getattr(raw, "tags", None) or [])[:16])
        size = attrs.get("Size")
        size_mb = round(float(size) / (1024**2), 1) if isinstance(size, (int, float)) else None
        created = str(attrs.get("Created"))[:32] if attrs.get("Created") else None
        return DockerImageSummary(id=short_id, tags=tags, size_mb=size_mb, created=created)

    @staticmethod
    def volume(raw: Any) -> DockerVolumeSummary:
        attrs = getattr(raw, "attrs", {}) or {}
        return DockerVolumeSummary(
            name=str(getattr(raw, "name", "unknown"))[:128],
            driver=str(attrs.get("Driver"))[:64] if attrs.get("Driver") else None,
            scope=str(attrs.get("Scope"))[:32] if attrs.get("Scope") else None,
        )

    @staticmethod
    def network(raw: Any) -> DockerNetworkSummary:
        attrs = getattr(raw, "attrs", {}) or {}
        return DockerNetworkSummary(
            name=str(getattr(raw, "name", "unknown"))[:128],
            driver=str(attrs.get("Driver"))[:64] if attrs.get("Driver") else None,
            scope=str(attrs.get("Scope"))[:32] if attrs.get("Scope") else None,
        )
