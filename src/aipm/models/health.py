from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class HealthState(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    UNKNOWN = "unknown"

@dataclass
class HealthCheckResult:
    component: str           # e.g., "supabase-db" or "Git State"
    state: HealthState
    message: str             # e.g., "Postgres accepting connections" or "Container exited with code 1"
    details: Optional[dict] = field(default_factory=dict)