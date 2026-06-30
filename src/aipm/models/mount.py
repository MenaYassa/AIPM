from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class MountInfo:
    source: str
    destination: str
    mode: str
    rw: bool