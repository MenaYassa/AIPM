from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class VolumeInfo:
    name: str
    driver: str
    mountpoint: str