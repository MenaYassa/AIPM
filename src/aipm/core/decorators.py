import functools
from rich.console import Console
from aipm.core.app import Application
from aipm.core.exceptions import AIPMError

def cli_handler(action_name: str):
    """
    A decorator that automatically handles logging and error catching for CLI capabilities.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            app = Application.create()
            console = Console()
            
            # Log the intent
            # args[1:] skips 'self', capturing the command arguments (like container name)
            params = [str(a) for a in args[1:]] + [f"{k}={v}" for k, v in kwargs.items()]
            param_str = ", ".join(params) if params else "None"
            
            app.logger.info(f"Executing '{action_name}' with params: [{param_str}]")
            
            try:
                # Execute the actual capability method
                return func(*args, **kwargs)
                
            except AIPMError as e:
                # Expected domain errors (e.g., ContainerNotFound)
                app.logger.warning(f"Action '{action_name}' failed: {e}")
                console.print(f"[red]Error:[/red] {e}")
                
            except Exception as e:
                # Unexpected crashes (e.g., network timeout, missing variable)
                app.logger.error(f"Critical error in '{action_name}': {e}", exc_info=True)
                console.print(f"[bold red]Critical System Error:[/bold red] {e}")
                console.print("[yellow]Check logs/aipm.log for full details.[/yellow]")
                
        return wrapper
    return decorator