"""Shared read-only Mission Control observation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Generic, TypeVar


class ObservationState(StrEnum):
    """Semantic state of an observation, independent of HTTP transport."""

    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    NEVER_SAMPLED = "never_sampled"
    UNKNOWN = "unknown"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ObservationError:
    """Safe, structured error information suitable for a response mapper."""

    code: str
    message: str


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Observation(Generic[T]):
    """Transport, availability, freshness, and payload state in one contract.

    ``transport_ok`` describes whether the request/adapter completed. ``available``
    describes whether a usable observation exists. ``state`` describes the
    observation semantics and is deliberately not collapsed into either boolean.
    """

    transport_ok: bool
    available: bool
    state: ObservationState
    data: T | None = None
    observed_at: datetime | None = None
    age_seconds: int | None = None
    max_age_seconds: int | None = None
    error: ObservationError | None = None

    @classmethod
    def from_sample(
        cls,
        data: T | None,
        *,
        observed_at: datetime | None,
        now: datetime,
        max_age_seconds: int,
        available: bool = True,
        transport_ok: bool = True,
        error: ObservationError | None = None,
    ) -> "Observation[T]":
        """Build a deterministic observation from a sample and its metadata."""

        if max_age_seconds < 0:
            raise ValueError("max_age_seconds must not be negative")
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if observed_at is not None and observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if not transport_ok or error is not None:
            return cls(
                transport_ok=transport_ok,
                available=False,
                state=ObservationState.ERROR,
                data=None,
                observed_at=observed_at,
                age_seconds=None,
                max_age_seconds=max_age_seconds,
                error=error,
            )
        if observed_at is None:
            return cls(
                transport_ok=True,
                available=available,
                state=ObservationState.NEVER_SAMPLED if available else ObservationState.UNAVAILABLE,
                data=data if available else None,
                max_age_seconds=max_age_seconds,
                error=error,
            )
        age_seconds = max(0, int((now - observed_at).total_seconds()))
        state = ObservationState.FRESH if available and age_seconds <= max_age_seconds else ObservationState.STALE if available else ObservationState.UNAVAILABLE
        return cls(
            transport_ok=True,
            available=available,
            state=state,
            data=data if available else None,
            observed_at=observed_at.astimezone(timezone.utc),
            age_seconds=age_seconds,
            max_age_seconds=max_age_seconds,
            error=error,
        )

    @classmethod
    def unknown(cls, *, data: T | None = None, error: ObservationError | None = None) -> "Observation[T]":
        """Represent a valid response whose semantic state is not known."""

        return cls(
            transport_ok=True,
            available=False,
            state=ObservationState.UNKNOWN,
            data=data,
            error=error,
        )
