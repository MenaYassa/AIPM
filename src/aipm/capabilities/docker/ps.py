from rich.console import Console
from rich.table import Table
from aipm.core.decorators import cli_handler
from aipm.core.app import Application


class PsCapability:
    
    @cli_handler(action_name="docker ps")
    def __init__(self):
        self.console = Console()
        self.app = Application.create()
        
    @cli_handler(action_name="docker ps")
    def ps(self):
        table = Table(title="Containers")
        # 1. You must define a column for every row item
        table.add_column("Name")
        table.add_column("Status")  # Shows state
        table.add_column("Health")  # Added this column
        table.add_column("Image")   # Added this column
        for container in self.app.docker.ps():
            table.add_row(
                container.name,
                container.state,
                container.health or "N/A",
                container.image,
            )

        self.console.print(table)