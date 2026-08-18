"""Typed read-only Docker detail projections for Mission Control."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aipm.models.telemetry import ResourceStats, TelemetryError


@dataclass(frozen=True, slots=True)
class DockerContainerDetail:
    id: str
    name: str
    project_key: str | None
    service_name: str | None
    image: str
    state: str
    health: str | None
    restart_count: int
    started_at: str | None
    ports: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    mount_kinds: tuple[str, ...] = ()
    resources: ResourceStats | None = None


@dataclass(frozen=True, slots=True)
class DockerImageSummary:
    id: str
    tags: tuple[str, ...]
    size_mb: float | None
    created: str | None


@dataclass(frozen=True, slots=True)
class DockerVolumeSummary:
    name: str
    driver: str | None
    scope: str | None


@dataclass(frozen=True, slots=True)
class DockerNetworkSummary:
    name: str
    driver: str | None
    scope: str | None


@dataclass(frozen=True, slots=True)
class DockerInventorySnapshot:
    available: bool
    status: str
    images: tuple[DockerImageSummary, ...] = ()
    volumes: tuple[DockerVolumeSummary, ...] = ()
    networks: tuple[DockerNetworkSummary, ...] = ()
    observed_at: datetime | None = None
    error: TelemetryError | None = None
