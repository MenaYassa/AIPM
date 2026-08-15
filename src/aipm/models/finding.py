from dataclasses import dataclass
from enum import Enum


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(slots=True, frozen=True)
class Finding:

    code: str

    component: str

    severity: Severity

    title: str

    description: str

    recommendation: str
    
    resource: str | None = None