from __future__ import annotations

from aipm.models.finding import Finding, Severity
from aipm.models.health import HealthState
from aipm.models.health_report import HealthReport
from aipm.models.recommendation import Recommendation


class ReportBuilder:
    _penalties = {
        Severity.INFO: 0,
        Severity.WARNING: 10,
        Severity.HIGH: 25,
        Severity.CRITICAL: 50,
    }

    def build(self, project, findings: list[Finding]) -> HealthReport:
        counts = {severity: sum(1 for finding in findings if finding.severity is severity) for severity in Severity}
        score = max(0, 100 - sum(self._penalties[finding.severity] for finding in findings))
        if counts[Severity.CRITICAL] or score < 60:
            state = HealthState.CRITICAL
        elif findings:
            state = HealthState.DEGRADED
        else:
            state = HealthState.HEALTHY

        recommendations: list[Recommendation] = []
        seen_actions: set[str] = set()
        for finding in sorted(findings, key=lambda item: (-self._severity_rank(item.severity), item.code)):
            action = finding.recommendation.strip()
            if action and action not in seen_actions:
                seen_actions.add(action)
                recommendations.append(
                    Recommendation(priority=self._severity_rank(finding.severity), action=action)
                )

        return HealthReport(
            project=project.name,
            score=score,
            state=state,
            critical=counts[Severity.CRITICAL],
            high=counts[Severity.HIGH],
            warning=counts[Severity.WARNING],
            info=counts[Severity.INFO],
            findings=findings,
            recommendations=recommendations,
        )

    @staticmethod
    def _severity_rank(severity: Severity) -> int:
        return {
            Severity.CRITICAL: 4,
            Severity.HIGH: 3,
            Severity.WARNING: 2,
            Severity.INFO: 1,
        }[severity]
