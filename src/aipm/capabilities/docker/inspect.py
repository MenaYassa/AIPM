import json
from rich.console import Console
from rich.table import Table
from aipm.mappers.docker import DockerMapper
from aipm.core.app import Application
from aipm.core.decorators import cli_handler # Import the decorator

class InspectCapability:
    def __init__(self):
        self.app = Application.create()

    @cli_handler(action_name="docker inspect")
    def inspect(self, name: str, raw: bool = False):
            # 1. Fetch
            container = self.app.docker.inspect(name)
            
            # 2. Handle Raw View
            if raw:
                print(json.dumps(container.attrs, indent=4))
                return

            # 3. Handle Pretty View
            data = DockerMapper.inspect_view(container)
            table = Table(title=f"Container: {data['name']}", show_header=False)
            table.add_column("Field", style="bold cyan")
            table.add_column("Value")
            
            table.add_row("Name", data["name"])
            table.add_row("Image", data["image"])
            table.add_row("Status", data["state"])
            table.add_row("Health", data["health"])
            table.add_row("IP Address", data["ip_address"])
            table.add_row("Created", data["created"])
            table.add_row("Command", data["command"])
            table.add_row("Ports", "\n".join(data["ports"]))
            table.add_row("Networks", ", ".join(data["networks"]))
            table.add_row("Mounts", "\n".join(data["mounts"]))
            table.add_row("Restart Policy", data["restart_policy"])
            
            Console().print(table)
            