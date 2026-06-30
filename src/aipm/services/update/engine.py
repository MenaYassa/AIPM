import subprocess
import socket
from pathlib import Path
from rich.console import Console
from aipm.services.project.service import ProjectService
from aipm.services.backup.engine import BackupEngine
from aipm.capabilities.health.diagnostics import HealthCapability
from aipm.core.exceptions import UpdateError

class UpdateEngine:
    def __init__(self):
        self.console = Console()
        self.project_service = ProjectService()
        self.backup_engine = BackupEngine()
        self.health_cap = HealthCapability()
        # Common ports used across your AI engineering infrastructure
        self.target_ports = [80, 443, 8080, 9000, 5432, 11434, 3000, 5678]

    def clear_port_blockers(self):
        """Audits target ports and force-kills conflicting containers/daemons."""
        self.console.print("[cyan]🔍 Performing pre-flight network port integrity check...[/cyan]")
        
        for port in self.target_ports:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                # If connect_ex returns 0, the port is occupied
                if s.connect_ex(('127.0.0.1', port)) == 0:
                    self.console.print(f"[yellow]⚠️ Port {port} is occupied. Clearing blocker...[/yellow]")
                    
                    # 1. Try to clear it via Docker filters if a container is binding it
                    subprocess.run(
                        f"docker rm -f $(docker ps -q --filter 'publish={port}') 2>/dev/null || true",
                        shell=True, capture_output=True
                    )
                    
                    # 2. Try to clear it via native process termination if it's a host zombie
                    subprocess.run(f"sudo fuser -k {port}/tcp 2>/dev/null || true", shell=True)

    def run_command_live(self, cmd: list, cwd: Path, step_name: str) -> str:
        """Executes a system shell command with live console feedback."""
        with self.console.status(f"[cyan]Executing: {' '.join(cmd)}...[/cyan]", spinner="dots"):
            result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            
        if result.returncode != 0:
            raise UpdateError(f"Step '{step_name}' failed!\nError Details: {result.stderr.strip()}")
        return result.stdout

    def execute_update(self, project_name: str):
        with self.console.status("[cyan]Loading project environment specifications...[/cyan]", spinner="dots"):
            project = self.project_service.get_project(project_name)
        
        project_path = Path(project.path)
        
        self.console.print(f"\n[bold magenta]🚀 Starting Transactional Update for:[/bold magenta] [bold white]{project.name}[/bold white]")
        self.console.print("[dim]------------------------------------------------------------[/dim]")

        # 1. Self-Healing: Clear out directory permissions and network blockers
        try:
            subprocess.run(["sudo", "chown", "-R", "ubuntu:ubuntu", str(project_path / "searxng")], capture_output=True)
            self.clear_port_blockers()
        except Exception as e:
            self.console.print(f"[dim][yellow]⚠️ Healing warning skipped: {e}[/dim]")

        # 2. Create Configuration Safety Snapshot
        self.console.print("[cyan]📦 Generating state safety-net configuration snapshot...[/cyan]")
        try:
            archive = self.backup_engine.create_snapshot(project)
            self.console.print(f"[green]✔ Snapshot successfully stored:[/green] [dim]{archive.archive_path}[/dim]")
        except Exception as e:
            raise UpdateError(f"Pre-update backup failed: {e}")

        # 3. Git Layer Update Transaction
        git_status_val = getattr(project, "git_status", getattr(project, "status", "unknown"))
        git_status_str = str(git_status_val).lower()
        
        # ONLY enforce Git rules if it is an actual git repository!
        if (project_path / ".git").exists():
            self.console.print(f"📋 Current Git State: [yellow]{git_status_val}[/yellow]")
            if any(keyword in git_status_str for keyword in ["dirty", "unsaved", "degraded", "uncommitted"]):
                self.console.print("\n[bold red]❌ UPDATE ABORTED:[/bold red] Project has uncommitted local file modifications.")
                self.console.print("[yellow]💡 Advice:[/yellow] Commit or stash your changes before running an update.\n")
                return
        else:
            self.console.print("📋 Current Git State: [dim white]N/A (Static Stack)[/dim white]")

        # 4. Infrastructure Layer Rebuild Transaction
        self.console.print("[cyan]🐳 Rebuilding and cycling container ecosystem layers...[/cyan]")
        if (project_path / "start_services.py").exists():
            self.run_command_live(["python3", "start_services.py"], cwd=project_path, step_name="Custom Runtime Rebuild")
        else:
            self.run_command_live(["docker", "compose", "up", "-d", "--build", "--remove-orphans"], cwd=project_path, step_name="Docker Compose Layer Build")
        
        self.console.print("[green]✔ Ecosystem deployed successfully.[/green]")

        # 5. Final Cluster Stability Verification
        self.console.print("[cyan]🩺 Handing over cluster context to Health Engine for target evaluation...[/cyan]\n")
        self.health_cap.check_health(project.name)
        self.console.print("\n[bold green]✨ Update transaction completed with no runtime faults detected![/bold green]\n")