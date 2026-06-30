from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    UNKNOWN = "unknown"

@dataclass
class ComposeService:
    name: str
    image: str
    state: str
    replicas: int = 1
    ports: list[str] = field(default_factory=list)

@dataclass
class ProjectCapabilities:
    has_compose: bool = False
    has_git: bool = False

@dataclass
class Project:
    name: str
    path: str
    capabilities: ProjectCapabilities = field(default_factory=ProjectCapabilities)
    compose_files: list[str] = field(default_factory=list)
    services: list[ComposeService] = field(default_factory=list)
    git_branch: Optional[str] = None
    git_dirty: Optional[bool] = None
    health: HealthStatus = HealthStatus.UNKNOWN