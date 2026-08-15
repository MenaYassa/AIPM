from aipm.engines.health.analyzers.base import Analyzer

from aipm.models.finding import Finding
from aipm.models.finding import Severity

from aipm.models.project import Project

from aipm.services.compose.service import ComposeService


class ComposeAnalyzer(Analyzer):

    def __init__(self):

        self.compose = ComposeService()

    def analyze(
        self,
        project: Project,
    ) -> list[Finding]:

        status = self.compose.status(project)

        findings: list[Finding] = []

        if status.running == 0:

            findings.append(

                Finding(

                    code="COMPOSE_DOWN",

                    component="Compose",

                    severity=Severity.HIGH,

                    title="Compose stack is down",

                    description="No running containers were found.",

                    recommendation="Start the stack using 'aipm compose up'.",

                )

            )

        if status.restarting:

            findings.append(

                Finding(

                    code="CONTAINERS_RESTARTING",

                    component="Compose",

                    severity=Severity.HIGH,

                    title="Restarting containers detected",

                    description=f"{status.restarting} container(s) are restarting.",

                    recommendation="Inspect the restarting containers.",

                )

            )

        if status.unhealthy:

            findings.append(

                Finding(

                    code="UNHEALTHY_CONTAINERS",

                    component="Compose",

                    severity=Severity.WARNING,

                    title="Unhealthy containers detected",

                    description=f"{status.unhealthy} unhealthy container(s).",

                    recommendation="Inspect container health checks.",

                )

            )

        return findings