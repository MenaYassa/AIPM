from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class DiskInfo:
    total_gb: float
    used_gb: float
    free_gb: float
    percent: float
