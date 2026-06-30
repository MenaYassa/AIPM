from rich.console import Console
from aipm.core.decorators import cli_handler
from aipm.core.app import Application


class RestartCapability:
    
    @cli_handler(action_name="docker restart")
    def __init__(self):
        self.console = Console()
        self.app = Application.create()
        
    @cli_handler(action_name="docker restart")
    def run(self, name: str):
        self.app.docker.restart(name)
        self.console.print(f"[green]{name} restarted[/green]")