from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class MemoryInfo:
    total_gb: float
    used_gb: float
    available_gb: float
    percent: float
