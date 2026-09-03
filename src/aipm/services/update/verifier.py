from __future__ import annotations

from aipm.models.finding import Finding, Severity
from aipm.models.health_report import HealthReport
from aipm.models.verification import UpdateVerification, UpdateVerificationStatus


class UpdateVerifier:
    """Independently verify a completed update by comparing health reports.

    Contract (grounded in the production roadmap's P1 "Update execution and
    verification" item):

    - The verifier is a distinct component from planning and execution. The
      planner never declares its own update successful and the executor is
      not responsible for determining its own success.
    - It compares the health-before and health-after reports of one update
      transaction and returns ``success``, ``warning``, or ``failure``.
    - Verdict semantics:

      ``SUCCESS``
          The health-after report contains no warning-or-worse findings.

      ``WARNING``
          The health-after report contains warning or high findings but no
          critical ones. Per the roadmap, warnings are not treated as
          rollback conditions: the update remains successful.

      ``FAILURE``
          The health-after report contains critical findings — the same
          condition that already triggers the engine's rollback path — or
          the verifier could not establish the health-after state at all
          (fail-safe: an unevaluatable outcome must not be reported as
          success).

    - Verification is strictly read-only and side-effect free: the verifier
      holds no services, providers, or runners and only compares the two
      reports the engine supplies. The engine produces the health-after
      report via its existing read-only ``HealthEngine``; the verifier itself
      runs no commands, performs no I/O, and can never mutate project or
      host state.
    """

    def verify_update(
        self,
        project_name: str,
        *,
        health_before: HealthReport | None,
        health_after: HealthReport | None,
    ) -> UpdateVerification:
        """Return the typed verdict for one update transaction."""
        try:
            return self._verify(health_before, health_after)
        except Exception as exc:
            return UpdateVerification(
                status=UpdateVerificationStatus.FAILURE,
                error=f"Verifier could not establish the post-update state: {exc}",
            )

    def _verify(
        self,
        health_before: HealthReport | None,
        health_after: HealthReport | None,
    ) -> UpdateVerification:
        if health_after is None:
            return UpdateVerification(
                status=UpdateVerificationStatus.FAILURE,
                error="Health-after report is missing; the update outcome cannot be verified.",
            )

        failures = _format(health_after, Severity.CRITICAL)
        if failures:
            return UpdateVerification(
                status=UpdateVerificationStatus.FAILURE,
                passed=_passed(health_after),
                failures=failures,
            )

        warnings = [*_format(health_after, Severity.HIGH), *_format(health_after, Severity.WARNING)]
        if warnings:
            return UpdateVerification(
                status=UpdateVerificationStatus.WARNING,
                passed=_passed(health_after),
                warnings=warnings,
            )

        return UpdateVerification(
            status=UpdateVerificationStatus.SUCCESS,
            passed=_passed(health_after),
        )


def _format(report: HealthReport, severity: Severity) -> list[str]:
    return [
        f"{finding.component}: {finding.title} ({severity.value})"
        for finding in report.findings
        if finding.severity is severity
    ]


def _passed(report: HealthReport) -> list[str]:
    return [
        f"{finding.component}: {finding.title}"
        for finding in report.findings
        if finding.severity is Severity.INFO
    ]
