from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from aipm.core.decorators import cli_handler
from aipm.engines.health.engine import HealthEngine
from aipm.services.project.service import ProjectService


class HealthCapability:
    def __init__(self, project_service: ProjectService | None = None, health_engine: HealthEngine | None = None):
        self.console = Console()
        self.project_service = project_service or ProjectService()
        self.health_engine = health_engine or HealthEngine()

    @cli_handler(action_name="aipm health")
    def check_health(self, project_name: str) -> None:
        project = self.project_service.get_project(project_name)
        self.console.print(f"[cyan]Running diagnostics for {project.name}...[/cyan]")
        report = self.health_engine.analyze(project)
        self.console.print(
            Panel.fit(
                "\n".join(
                    [
                        f"Project      : {report.project}",
                        f"Score        : {report.score}/100",
                        f"State        : {report.state.value.upper()}",
                        f"Findings     : {len(report.findings)}",
                        f"Critical/High: {report.critical}/{report.high}",
                        f"Warning/Info : {report.warning}/{report.info}",
                    ]
                )
            )
        )
        if report.findings:
            table = Table(title="Findings")
            table.add_column("Severity")
            table.add_column("Component")
            table.add_column("Title")
            table.add_column("Recommendation")
            for finding in report.findings:
                table.add_row(
                    finding.severity.value.upper(),
                    finding.component,
                    finding.title,
                    finding.recommendation,
                )
            self.console.print(table)
        else:
            self.console.print("[bold green]No findings detected.[/bold green]")

        if report.recommendations:
            recommendations = Table(title="Prioritized recommendations")
            recommendations.add_column("Priority", justify="right")
            recommendations.add_column("Action")
            for recommendation in report.recommendations:
                recommendations.add_row(str(recommendation.priority), recommendation.action)
            self.console.print(recommendations)