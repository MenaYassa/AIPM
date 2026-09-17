"""Two-level candidate query budget model: logical lookups vs physical network operations.

Ensures candidate intelligence remains strictly bounded across:
1. Level 1: Logical candidate lookups (unique image reference queries).
2. Level 2: Physical network operations (individual HTTP request/response exchanges).
3. Monotonic wall-clock deadline across the entire observation cycle.
4. Response byte limits (individual response cap and aggregate memory bound).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from aipm.models.compose_intelligence import ServiceCandidateReason

DEFAULT_MAX_LOGICAL_LOOKUPS = 25
DEFAULT_MAX_NETWORK_OPS = 50
DEFAULT_TOTAL_DEADLINE_SECONDS = 8.0
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024        # 1 MB
DEFAULT_MAX_AGGREGATE_BYTES = 10 * 1024 * 1024  # 10 MB


@dataclass
class TwoLevelBudget:
    """Manages and enforces the two-level budget and monotonic deadline."""

    max_logical_lookups: int = DEFAULT_MAX_LOGICAL_LOOKUPS
    max_network_ops: int = DEFAULT_MAX_NETWORK_OPS
    total_deadline_seconds: float = DEFAULT_TOTAL_DEADLINE_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    max_aggregate_bytes: int = DEFAULT_MAX_AGGREGATE_BYTES

    logical_lookups_performed: int = 0
    network_ops_performed: int = 0
    aggregate_bytes_received: int = 0
    start_monotonic: float = 0.0

    def __post_init__(self) -> None:
        if self.start_monotonic == 0.0:
            self.start_monotonic = time.monotonic()
        if not isinstance(self.max_logical_lookups, int):
            self.max_logical_lookups = DEFAULT_MAX_LOGICAL_LOOKUPS
        if not isinstance(self.max_network_ops, int):
            self.max_network_ops = DEFAULT_MAX_NETWORK_OPS

    @property
    def deadline_monotonic(self) -> float:
        """Absolute monotonic deadline timestamp."""
        return self.start_monotonic + self.total_deadline_seconds

    def remaining_time(self, now: float | None = None) -> float:
        """Return remaining seconds until the monotonic deadline."""
        current = time.monotonic() if now is None else now
        return max(0.0, self.deadline_monotonic - current)

    def check_logical_lookup(self, now: float | None = None) -> tuple[bool, ServiceCandidateReason | None, str | None]:
        """Check whether a new logical candidate lookup is permitted."""
        current = time.monotonic() if now is None else now
        if current >= self.deadline_monotonic:
            return (
                False,
                ServiceCandidateReason.REGISTRY_TIMEOUT,
                f"Registry request timed out: operation deadline ({self.total_deadline_seconds}s) exceeded",
            )
        if self.logical_lookups_performed >= self.max_logical_lookups:
            return (
                False,
                ServiceCandidateReason.BUDGET_EXHAUSTED,
                f"Logical candidate lookup budget reached ({self.max_logical_lookups})",
            )
        if self.network_ops_performed >= self.max_network_ops:
            return (
                False,
                ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED,
                f"Physical network operations budget reached ({self.max_network_ops})",
            )
        return True, None, None

    def record_logical_lookup(self) -> None:
        """Increment the count of started logical candidate lookups."""
        self.logical_lookups_performed += 1

    def check_network_op(self, now: float | None = None) -> tuple[bool, ServiceCandidateReason | None, str | None]:
        """Check whether an individual physical HTTP operation is permitted."""
        current = time.monotonic() if now is None else now
        if current >= self.deadline_monotonic:
            return (
                False,
                ServiceCandidateReason.REGISTRY_TIMEOUT,
                f"Registry request timed out: operation deadline ({self.total_deadline_seconds}s) exceeded",
            )
        if self.network_ops_performed >= self.max_network_ops:
            return (
                False,
                ServiceCandidateReason.NETWORK_BUDGET_EXHAUSTED,
                f"Physical network operations budget reached ({self.max_network_ops})",
            )
        return True, None, None

    def record_network_op(self, bytes_received: int) -> None:
        """Record a completed physical HTTP operation and enforce aggregate byte limits."""
        self.network_ops_performed += 1
        self.aggregate_bytes_received += bytes_received
        if self.aggregate_bytes_received > self.max_aggregate_bytes:
            raise ValueError(
                f"Aggregate response bytes ({self.aggregate_bytes_received}) exceeded limit ({self.max_aggregate_bytes} bytes)"
            )

    def stats(self) -> dict[str, int | float]:
        """Return operational budget statistics."""
        return {
            "logical_lookups_performed": self.logical_lookups_performed,
            "max_logical_lookups": self.max_logical_lookups,
            "network_ops_performed": self.network_ops_performed,
            "max_network_ops": self.max_network_ops,
            "aggregate_bytes_received": self.aggregate_bytes_received,
            "max_aggregate_bytes": self.max_aggregate_bytes,
            "remaining_time_seconds": self.remaining_time(),
        }
