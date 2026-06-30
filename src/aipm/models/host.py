from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class HostInfo:
    hostname: str
    os: str
    kernel: str
    architecture: str
    python: str
