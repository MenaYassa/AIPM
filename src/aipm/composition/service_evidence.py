"""Composition-root verifier connecting Compose observation to FinalExecutionGate.

Binds the control plane's FinalExecutionGate to the live Compose planning mechanics.
Lives at the composition root to preserve clean separation between authority (control plane)
and mechanics (services/providers).
"""
from __future__ import annotations

from typing import Any, Callable

from aipm.control_plane.gate import GateCode, verify_service_evidence
from aipm.models.compose_plan import ServiceEvidenceContract


class ComposeServiceEvidenceVerifier:
    """Re-observes live Compose state and verifies against authorized service evidence."""

    def __init__(
        self,
        *,
        compose_service: Any,
        project_resolver: Callable[[str], Any],
    ) -> None:
        self._compose_service = compose_service
        self._project_resolver = project_resolver

    def __call__(self, contract: Any, authorized_evidence: Any, *, now: Any = None) -> GateCode:
        return self.verify(contract, authorized_evidence, now=now)

    def verify(self, contract: Any, authorized_evidence: Any, *, now: Any = None) -> GateCode:
        target_id = getattr(contract, "target_id", None) or getattr(authorized_evidence, "project_name", "")
        service_name = getattr(authorized_evidence, "service_name", "")
        if not target_id or not service_name:
            return GateCode.PLAN_IDENTITY_MISMATCH

        try:
            project = self._project_resolver(target_id)
        except Exception:
            return GateCode.TARGET_MISMATCH
        if project is None:
            return GateCode.TARGET_MISMATCH

        try:
            fresh_plan = self._compose_service.plan_service_update(
                project,
                service_name,
                query_registries=True,
            )
        except Exception:
            return GateCode.STALE_PLAN

        fresh_evidence = ServiceEvidenceContract.from_service_plan(
            fresh_plan,
            requested_scope=getattr(authorized_evidence, "requested_scope", (service_name,)),
        )

        expected_digest = getattr(contract, "expected_plan_digest", None)
        return verify_service_evidence(
            authorized=authorized_evidence,
            fresh=fresh_evidence,
            expected_digest=expected_digest,
        )


def compose_service_evidence_verifier(
    compose_service: Any,
    project_resolver: Callable[[str], Any],
) -> ComposeServiceEvidenceVerifier:
    return ComposeServiceEvidenceVerifier(
        compose_service=compose_service,
        project_resolver=project_resolver,
    )
