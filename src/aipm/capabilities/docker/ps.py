from __future__ import annotations

from rich.console import Console
from rich.table import Table

from aipm.core.app import Application
from aipm.core.decorators import cli_handler


class PsCapability:
    def __init__(self):
        self.console = Console()
        self.app = Application.create()

    @cli_handler(action_name="docker ps")
    def ps(self) -> None:
        table = Table(title="Containers")
        table.add_column("Name")
        table.add_column("Status")
        table.add_column("Health")
        table.add_column("Image")
        for container in self.app.docker.ps():
            table.add_row(
                container.name,
                container.state,
                container.health or "N/A",
                container.image,
            )
        self.console.print(table)
