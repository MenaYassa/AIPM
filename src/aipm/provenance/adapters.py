"""Initial MC-6.12B observation adapters.

Only HOST is implemented initially. Systemd and Docker are deliberately absent
from this module and therefore unavailable by construction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psutil

from aipm.control_plane.models import EvidenceState, EvidenceSummary


@dataclass(frozen=True, slots=True)
class HostObservation:
    state: EvidenceState
    observed_at: datetime
    freshness_deadline: datetime
    evidence: EvidenceSummary


class HostAdapter:
    """Read-only bounded host adapter with no shell, subprocess, or network."""

    source_id = "host"

    def observe(self, *, now: datetime, max_age_seconds: int = 60) -> HostObservation:
        current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        items: list[tuple[str, str]] = []
        try:
            items.extend((
                ("cpu_count", str(min(psutil.cpu_count(logical=True) or 0, 1024))),
                ("memory_bytes", str(min(psutil.virtual_memory().total, 1 << 60))),
                ("load_1m", f"{min(max(psutil.getloadavg()[0], 0.0), 1_000_000.0):.3f}"),
            ))
            evidence = EvidenceSummary(EvidenceState.OBSERVED, tuple(items))
            return HostObservation(EvidenceState.OBSERVED, current, current + timedelta(seconds=max_age_seconds), evidence)
        except Exception:
            evidence = EvidenceSummary(EvidenceState.UNAVAILABLE, ())
            return HostObservation(EvidenceState.UNAVAILABLE, current, current, evidence)
