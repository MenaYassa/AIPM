from rich.console import Console
from aipm.core.decorators import cli_handler
from aipm.core.app import Application


class StopCapability:
    @cli_handler(action_name="docker stop")
    def __init__(self):
        self.console = Console()
        self.app = Application.create()
        
    @cli_handler(action_name="docker stop")
    def run(self, name: str):
        self.app.docker.stop(name)
        self.console.print(f"[red]{name} stopped[/red]")