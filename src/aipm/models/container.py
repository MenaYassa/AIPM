from dataclasses import dataclass
from datetime import datetime  # <--- Add this import
from aipm.models.mount import MountInfo
from aipm.models.port import PortInfo

@dataclass(slots=True, frozen=True)
class Container:
    id: str
    name: str
    image: str
    state: str
    health: str | None
    ports: list[str]
    labels: dict[str, str]
    stack: str | None
    created: datetime