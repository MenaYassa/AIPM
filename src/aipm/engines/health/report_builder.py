from aipm.models.health import HealthState
from aipm.models.health_report import HealthReport
from aipm.models.finding import Severity



class ReportBuilder:

    def build(

        self,

        project,

        findings,

    ):


        critical = sum(
            1
            for f in findings
            if f.severity == Severity.CRITICAL
        )

        high = sum(
            1
            for f in findings
            if f.severity == Severity.HIGH
        )

        warning = sum(
            1
            for f in findings
            if f.severity == Severity.WARNING
        )

        info = sum(
            1
            for f in findings
            if f.severity == Severity.INFO
        )
        score = 100

        for finding in findings:

            match finding.severity.value:

                case "warning":

                    score -= 10

                case "high":

                    score -= 25

                case "critical":

                    score -= 50

        score = max(score, 0)

        if score >= 90:

            state = HealthState.HEALTHY

        elif score >= 60:

            state = HealthState.DEGRADED

        else:

            state = HealthState.CRITICAL

        return HealthReport(

            project=project.name,

            score=score,

            state=state,

            critical=critical,

            high=high,

            warning=warning,

            info=info,

            findings=findings,

            recommendations=[],
            

        )