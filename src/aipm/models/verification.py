from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class UpdateVerificationStatus(Enum):
    """Typed verdict of the independent post-update verifier."""

    SUCCESS = "success"
    WARNING = "warning"
    FAILURE = "failure"


@dataclass(slots=True, frozen=True)
class UpdateVerification:
    """Outcome of independently verifying a completed update.

    Produced by ``UpdateVerifier`` from the health-before and health-after
    reports of one update transaction. This is the update-engine verdict and
    is deliberately distinct from the control plane's action verification
    models, which classify control-plane plan readbacks rather than
    ``aipm update`` health outcomes.
    """

    status: UpdateVerificationStatus
    passed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def update_successful(self) -> bool:
        """Whether the update should be considered successful.

        WARNING verdicts still count as successful: per the roadmap they are
        not rollback conditions.
        """
        return self.status is not UpdateVerificationStatus.FAILURE

    @property
    def rollback_required(self) -> bool:
        """Whether this verdict requires the engine's rollback path."""
        return self.status is UpdateVerificationStatus.FAILURE
