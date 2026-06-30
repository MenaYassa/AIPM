from rich.console import Console
from rich.table import Table
from aipm.core.app import Application
from aipm.core.decorators import cli_handler

class SystemCapability:
    def __init__(self):
        self.app = Application.create()
        self.console = Console()

    @cli_handler(action_name="docker logs")
    def logs(self, name: str, tail: int):
        logs_output = self.app.docker.logs(name, tail)
        self.console.print(f"[bold cyan]Logs for {name} (last {tail} lines):[/bold cyan]")
        self.console.print(logs_output)

    @cli_handler(action_name="docker images")
    def images(self):
        images_data = self.app.docker.images()
        table = Table(title="Docker Images")
        table.add_column("ID", style="cyan")
        table.add_column("Tags", style="green")
        table.add_column("Size", style="yellow")
        table.add_column("Created")

        for img in images_data:
            table.add_row(img["id"], img["tags"], img["size"], img["created"])
        
        self.console.print(table)

    @cli_handler(action_name="docker volumes")
    def volumes(self):
        volumes_data = self.app.docker.volumes()
        table = Table(title="Docker Volumes")
        table.add_column("Name", style="cyan")
        table.add_column("Driver", style="green")
        table.add_column("Mountpoint")

        for vol in volumes_data:
            table.add_row(vol["name"], vol["driver"], vol["mountpoint"])
        
        self.console.print(table)

    @cli_handler(action_name="docker networks")
    def networks(self):
        networks_data = self.app.docker.networks()
        table = Table(title="Docker Networks")
        table.add_column("Name", style="cyan")
        table.add_column("Driver", style="green")
        table.add_column("Scope")

        for net in networks_data:
            table.add_row(net["name"], net["driver"], net["scope"])
        
        self.console.print(table)