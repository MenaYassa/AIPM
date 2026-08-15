from dataclasses import dataclass, field

from aipm.models.container import Container


@dataclass(slots=True, frozen=True)
class ComposeStatus:

    project_name: str

    compose_files: list[str] = field(default_factory=list)

    containers: list[Container] = field(default_factory=list)

    running: int = 0

    stopped: int = 0

    restarting: int = 0

    unhealthy: int = 0