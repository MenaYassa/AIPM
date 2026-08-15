from dataclasses import dataclass, field

from aipm.models.finding import Finding
from aipm.models.recommendation import Recommendation
from aipm.models.health import HealthState


@dataclass(slots=True)
class HealthReport:

    project: str

    score: int

    state: HealthState

    critical: int = 0

    high: int = 0

    warning: int = 0

    info: int = 0

    findings: list[Finding] = field(default_factory=list)

    recommendations: list[Recommendation] = field(default_factory=list)