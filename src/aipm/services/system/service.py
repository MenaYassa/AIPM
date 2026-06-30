import platform
import socket

import psutil

from aipm.models.cpu import CpuInfo
from aipm.models.disk import DiskInfo
from aipm.models.host import HostInfo
from aipm.models.memory import MemoryInfo
from aipm.models.system import SystemSummary


class SystemService:

    def hostname(self):

        return socket.gethostname()

    def os(self):

        return platform.system()

    def kernel(self):

        return platform.release()

    def architecture(self):

        return platform.machine()

    def python(self):

        return platform.python_version()

    def cpu(self):

        return CpuInfo(
            physical_cores=psutil.cpu_count(False),
            logical_cores=psutil.cpu_count(True),
            usage_percent=psutil.cpu_percent(interval=0.5),
        )

    def memory(self):

        mem = psutil.virtual_memory()

        gb = 1024 ** 3

        return MemoryInfo(
            total_gb=mem.total / gb,
            used_gb=mem.used / gb,
            available_gb=mem.available / gb,
            percent=mem.percent,
        )

    def disk(self):

        disk = psutil.disk_usage("/")

        gb = 1024 ** 3

        return DiskInfo(
            total_gb=disk.total / gb,
            used_gb=disk.used / gb,
            free_gb=disk.free / gb,
            percent=disk.percent,
        )

    def summary(self):

        return SystemSummary(
            host=self.host(),
            cpu=self.cpu(),
            memory=self.memory(),
            disk=self.disk(),
        )
    
    def host(self):

        return HostInfo(
            hostname=self.hostname(),
            os=self.os(),
            kernel=self.kernel(),
            architecture=self.architecture(),
            python=self.python(),
        )