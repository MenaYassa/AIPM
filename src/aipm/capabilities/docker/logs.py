from rich.console import Console
from aipm.core.decorators import cli_handler
from aipm.core.app import Application


class LogsCapability:
    
    @cli_handler(action_name="docker logs")
    def __init__(self):
        self.console = Console()
        self.app = Application.create()
        
    @cli_handler(action_name="docker logs")
    def run(self, name: str, tail: int):
        logs = self.app.docker.logs(name, tail)
        self.console.print(logs)