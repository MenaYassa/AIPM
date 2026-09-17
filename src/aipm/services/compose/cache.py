"""Bounded LRU cache for OCI registry candidate digests with freshness semantics.

Ensures candidate lookups are bounded in memory, support deterministic eviction,
distinguish between fresh and stale observations, and never replace fresh
runtime evidence.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aipm.models.compose_intelligence import (
    CandidateCacheFreshness,
    CandidateLookupKey,
)

if TYPE_CHECKING:
    from aipm.services.compose.registry_client import RegistryCandidateResult

DEFAULT_CACHE_CAPACITY = 256
DEFAULT_POSITIVE_TTL_SECONDS = 900.0  # 15 minutes
DEFAULT_NEGATIVE_TTL_SECONDS = 60.0   # 1 minute


@dataclass(frozen=True, slots=True)
class CachedCandidateEntry:
    """Immutable entry stored in the candidate cache."""

    key: CandidateLookupKey
    result: RegistryCandidateResult
    cached_at: float
    ttl_seconds: float
    is_negative: bool = False

    def is_fresh(self, now: float) -> bool:
        """Check if entry is within its active time-to-live window."""
        return (now - self.cached_at) <= self.ttl_seconds

    def freshness(self, now: float) -> CandidateCacheFreshness:
        """Compute freshness status enum."""
        if self.is_fresh(now):
            return CandidateCacheFreshness.FRESH
        return CandidateCacheFreshness.STALE

    def age(self, now: float) -> float:
        """Return the age of the entry in seconds."""
        return max(0.0, now - self.cached_at)


class CandidateCache:
    """Bounded, thread-safe LRU cache for candidate image results."""

    def __init__(
        self,
        *,
        max_capacity: int = DEFAULT_CACHE_CAPACITY,
        positive_ttl_seconds: float = DEFAULT_POSITIVE_TTL_SECONDS,
        negative_ttl_seconds: float = DEFAULT_NEGATIVE_TTL_SECONDS,
    ) -> None:
        self.max_capacity = max(1, max_capacity)
        self.positive_ttl_seconds = positive_ttl_seconds
        self.negative_ttl_seconds = negative_ttl_seconds
        self._entries: OrderedDict[CandidateLookupKey, CachedCandidateEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(
        self,
        key: CandidateLookupKey,
        *,
        now: float | None = None,
    ) -> tuple[RegistryCandidateResult, CandidateCacheFreshness] | None:
        """Retrieve a cached entry, updating LRU order."""
        current_time = time.monotonic() if now is None else now
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None

            # Move to end for LRU tracking
            self._entries.move_to_end(key)
            self._hits += 1
            return entry.result, entry.freshness(current_time)

    def peek(
        self,
        key: CandidateLookupKey,
        *,
        now: float | None = None,
    ) -> tuple[RegistryCandidateResult, CandidateCacheFreshness] | None:
        """Inspect a cached entry without mutating LRU order or hit/miss statistics."""
        current_time = time.monotonic() if now is None else now
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            return entry.result, entry.freshness(current_time)

    def put(
        self,
        key: CandidateLookupKey,
        result: RegistryCandidateResult,
        *,
        ttl_seconds: float | None = None,
        is_negative: bool = False,
        now: float | None = None,
    ) -> None:
        """Store a candidate result, evicting oldest entry if at capacity."""
        current_time = time.monotonic() if now is None else now
        if ttl_seconds is None:
            ttl_seconds = self.negative_ttl_seconds if is_negative else self.positive_ttl_seconds

        entry = CachedCandidateEntry(
            key=key,
            result=result,
            cached_at=current_time,
            ttl_seconds=ttl_seconds,
            is_negative=is_negative,
        )

        with self._lock:
            if key in self._entries:
                del self._entries[key]
            elif len(self._entries) >= self.max_capacity:
                self._entries.popitem(last=False)
                self._evictions += 1

            self._entries[key] = entry

    def evict(self, key: CandidateLookupKey) -> bool:
        """Explicitly evict a specific key from cache."""
        with self._lock:
            if key in self._entries:
                del self._entries[key]
                return True
            return False

    def clear(self) -> None:
        """Clear all entries and reset hit/miss counters."""
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def stats(self) -> dict[str, int]:
        """Return operational statistics for the cache."""
        with self._lock:
            return {
                "size": len(self._entries),
                "max_capacity": self.max_capacity,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
            }
