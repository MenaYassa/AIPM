from dataclasses import dataclass

from aipm.models.cpu import CpuInfo
from aipm.models.disk import DiskInfo
from aipm.models.host import HostInfo
from aipm.models.memory import MemoryInfo

@dataclass(slots=True, frozen=True)
class SystemSummary:

    host: HostInfo

    cpu: CpuInfo

    memory: MemoryInfo

    disk: DiskInfo
