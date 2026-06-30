from rich.console import Console
from aipm.core.decorators import cli_handler
from aipm.core.app import Application


class StartCapability:
    @cli_handler(action_name="docker start")
    def __init__(self):
        self.console = Console()
        self.app = Application.create()
        
    @cli_handler(action_name="docker start")
    def run(self, name: str):
        self.app.docker.start(name)
        self.console.print(f"[green]{name} started[/green]")