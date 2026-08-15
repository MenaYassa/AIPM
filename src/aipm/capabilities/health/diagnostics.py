from rich.console import Console
from rich.table import Table
from aipm.core.decorators import cli_handler
from aipm.services.project.service import ProjectService
from aipm.models.health import HealthState
from aipm.engines.health.engine import HealthEngine

class HealthCapability:

    def __init__(self):

        self.console = Console()

        self.project_service = ProjectService()

        self.health_engine = HealthEngine()

    @cli_handler(action_name="aipm health")
    def check_health(self, project_name: str):
        project = self.project_service.get_project(project_name)

        report = self.health_engine.analyze(project)

        self.console.print(
            f"[cyan]Running diagnostics for {project.name}...[/cyan]\n"
        )
        
        from rich.panel import Panel
        from rich.table import Table

        self.console.print(
            Panel.fit(
                f"""
        Project : {report.project}

        Score   : {report.score}/100

        State   : {report.state.name}

        Findings: {len(report.findings)}
        """
            )
        )

        if not report.findings:

            self.console.print("[bold green]✓ No findings detected.[/bold green]")

            return

        table = Table(title="Findings")

        table.add_column("Severity")

        table.add_column("Component")

        table.add_column("Title")

        table.add_column("Recommendation")

        for finding in report.findings:

            table.add_row(

                finding.severity.name,

                finding.component,

                finding.title,

                finding.recommendation,

            )

        self.console.print(table)