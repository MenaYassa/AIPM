from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class CpuInfo:
    physical_cores: int
    logical_cores: int
    usage_percent: float
