"""Reusable bounds for read-only Mission Control queries."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_MAX_LIMIT = 500
MAX_LIMIT = 5000
MAX_OFFSET = 100_000
MAX_CURSOR_LENGTH = 256
MAX_FILTER_LENGTH = 256
MAX_LOG_LINES = 2_000
MAX_LOG_BYTES = 1_000_000

SUPPORTED_RANGES: dict[str, int] = {
    "1h": 3_600,
    "6h": 21_600,
    "24h": 86_400,
    "7d": 604_800,
}


@dataclass(frozen=True, slots=True)
class BoundedQuery:
    """Normalized bounded query values shared by read façades."""

    range_name: str = "24h"
    limit: int = DEFAULT_MAX_LIMIT
    offset: int = 0
    cursor: str | None = None

    @property
    def range_seconds(self) -> int:
        return SUPPORTED_RANGES[self.range_name]


def validate_range_name(value: str = "24h") -> str:
    if not isinstance(value, str) or value not in SUPPORTED_RANGES:
        raise ValueError(f"range must be one of: {', '.join(SUPPORTED_RANGES)}")
    return value


def validate_limit(value: int = DEFAULT_MAX_LIMIT, *, maximum: int = MAX_LIMIT) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("limit must be an integer")
    if maximum <= 0 or maximum > MAX_LIMIT:
        raise ValueError("maximum limit is outside the supported bounds")
    if value < 1 or value > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
    return value


def validate_offset(value: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("offset must be an integer")
    if value < 0 or value > MAX_OFFSET:
        raise ValueError(f"offset must be between 0 and {MAX_OFFSET}")
    return value


def validate_cursor(value: str | None = None) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_CURSOR_LENGTH or any(char.isspace() for char in value):
        raise ValueError(f"cursor must be a non-whitespace value of at most {MAX_CURSOR_LENGTH} characters")
    return value


def validate_filter(value: str | None = None, *, maximum: int = MAX_FILTER_LENGTH) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"filter must be at most {maximum} characters")
    return value


def validate_log_lines(value: int = 200) -> int:
    return validate_limit(value, maximum=MAX_LOG_LINES)


def validate_log_bytes(value: int = 100_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > MAX_LOG_BYTES:
        raise ValueError(f"bytes must be between 1 and {MAX_LOG_BYTES}")
    return value


def bounded_query(
    *,
    range_name: str = "24h",
    limit: int = DEFAULT_MAX_LIMIT,
    offset: int = 0,
    cursor: str | None = None,
) -> BoundedQuery:
    """Validate and normalize a read query without touching any data source."""

    return BoundedQuery(
        range_name=validate_range_name(range_name),
        limit=validate_limit(limit),
        offset=validate_offset(offset),
        cursor=validate_cursor(cursor),
    )
