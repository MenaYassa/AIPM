from rich.console import Console
from rich.table import Table

from aipm.core.app import Application


class DoctorCapability:

    def __init__(self):

        self.console = Console()
        self.app = Application.create()

    def run(self):

        system = self.app.system.summary()

        host = system.host
        cpu = system.cpu
        memory = system.memory
        disk = system.disk

        table = Table(title="System")

        table.add_column("Property", style="cyan")
        table.add_column("Value", style="green")

        # Host
        table.add_row("Hostname", host.hostname)
        table.add_row("OS", host.os)
        table.add_row("Kernel", host.kernel)
        table.add_row("Architecture", host.architecture)
        table.add_row("Python", host.python)

        table.add_section()

        # CPU
        table.add_row("CPU (Physical)", str(cpu.physical_cores))
        table.add_row("CPU (Logical)", str(cpu.logical_cores))
        table.add_row("CPU Usage", f"{cpu.usage_percent:.1f}%")

        table.add_section()

        # Memory
        table.add_row(
            "Memory",
            f"{memory.used_gb:.1f}/{memory.total_gb:.1f} GB ({memory.percent:.1f}%)",
        )

        table.add_row(
            "Memory Available",
            f"{memory.available_gb:.1f} GB",
        )

        table.add_section()

        # Disk
        table.add_row(
            "Disk",
            f"{disk.used_gb:.1f}/{disk.total_gb:.1f} GB ({disk.percent:.1f}%)",
        )

        table.add_row(
            "Disk Free",
            f"{disk.free_gb:.1f} GB",
        )

        self.console.print(table)
