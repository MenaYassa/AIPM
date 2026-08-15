from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
from aipm.models.git import GitRepository

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
    git: GitRepository | None = None
    health: HealthStatus = HealthStatus.UNKNOWN

    @property
    def git_branch(self) -> str | None:
        """Compatibility wrapper for old code expecting project.git_branch."""
        return self.git.branch if self.git else None

    @property
    def git_dirty(self) -> bool:
        """Compatibility wrapper for old code expecting project.git_dirty."""
        return self.git.dirty if self.git else False