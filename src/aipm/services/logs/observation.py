from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from aipm.models.logs import (
    LogCursor,
    LogEntry,
    LogErrorCode,
    LogObservationError,
    LogPage,
    LogQuery,
    LogSeverity,
    LogSource,
    ProviderLogLine,
)
from aipm.models.mission_control import Observation, ObservationError, ObservationState
from aipm.providers.logs import LogProvider, LogProviderError


_REDACTIONS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("authorization", re.compile(r"(?i)\b(authorization\s*[:=]\s*|bearer\s+)(?:bearer\s+)?[^\s,;]+"), r"\1[REDACTED_AUTH]"),
    ("credential", re.compile(r"(?i)\b(password|passwd|secret|credential|token|api[_-]?key)\s*[:=]\s*[^\s,;]+"), r"\1=[REDACTED_SECRET]"),
    ("environment", re.compile(r"(?i)\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|AUTH|CREDENTIAL)[A-Z0-9_]*\s*=\s*[^\s,;]+"), "[REDACTED_ENVIRONMENT]"),
    ("destination", re.compile(r"(?i)https?://[^\s,;]+"), "[REDACTED_URL]"),
    ("private_path", re.compile(r"(?:/home/[^\s,;]+|/root/[^\s,;]+|/etc/[^\s,;]+|/var/lib/[^\s,;]+)"), "[REDACTED_PATH]"),
    ("command", re.compile(r"(?i)(?:^|\s)(?:/usr/bin/|/bin/|sudo\s+|sh\s+-c\s+)[^\n]+"), " [REDACTED_COMMAND]"),
)


class ReadOnlyLogService:
    """Bounded log orchestration; providers never receive browser-owned source data."""

    def __init__(
        self,
        registry: Mapping[str, LogSource],
        providers: Mapping[str, LogProvider],
        *,
        now: Callable[[], datetime] | None = None,
        max_age_seconds: int = 90,
    ) -> None:
        self.registry = dict(registry)
        self.providers = dict(providers)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.max_age_seconds = max_age_seconds

    def read(self, query: LogQuery) -> Observation[LogPage]:
        source = self.registry.get(query.source_id)
        if source is None:
            error = LogObservationError(LogErrorCode.UNKNOWN_SOURCE, "Log source is not allow-listed")
            return _error_observation(error)
        if query.cursor is not None and (
            query.cursor.source_id != source.id or query.cursor.fingerprint != query.fingerprint()
        ):
            error = LogObservationError(LogErrorCode.INVALID_CURSOR, "Cursor does not match this bounded log query")
            return _error_observation(error)
        provider = self.providers.get(source.kind.value)
        if provider is None:
            error = LogObservationError(LogErrorCode.SOURCE_UNAVAILABLE, "Log source is unavailable")
            return _unavailable_observation(error)
        try:
            raw_lines = provider.read(source, query)
        except LogProviderError:
            error = LogObservationError(LogErrorCode.SOURCE_UNAVAILABLE, "Log source is unavailable")
            return _unavailable_observation(error)
        except Exception:
            error = LogObservationError(LogErrorCode.SOURCE_FAILED, "Log source failed safely")
            return _error_observation(error)

        selected = [line for line in raw_lines if query.since <= line.observed_at <= query.until]
        if query.severity is not None:
            selected = [line for line in selected if line.severity == query.severity]
        if query.unit_id is not None:
            selected = [line for line in selected if line.unit_id == query.unit_id]
        if query.project_id is not None:
            selected = [line for line in selected if line.project_id == query.project_id]
        start = query.cursor.offset if query.cursor else 0
        selected = selected[start:]
        entries: list[LogEntry] = []
        returned_bytes = 0
        truncated = False
        for line in selected:
            message, evidence = redact_message(line.message)
            encoded_bytes = len(message.encode("utf-8"))
            if len(entries) >= query.limit or returned_bytes + encoded_bytes > query.max_bytes:
                truncated = True
                break
            entries.append(
                LogEntry(
                    timestamp=line.observed_at.astimezone(timezone.utc),
                    severity=line.severity,
                    message=message,
                    redacted=bool(evidence),
                    evidence=tuple(evidence),
                    unit_id=line.unit_id,
                    project_id=line.project_id,
                )
            )
            returned_bytes += encoded_bytes
        next_cursor = None
        if truncated:
            next_cursor = LogCursor(source.id, start + len(entries), query.fingerprint()).encode()
        page = LogPage(
            source=source,
            entries=tuple(entries),
            next_cursor=next_cursor,
            truncated=truncated,
            returned_lines=len(entries),
            returned_bytes=returned_bytes,
        )
        observed_at = self.now().astimezone(timezone.utc)
        return Observation.from_sample(
            page,
            observed_at=observed_at,
            now=observed_at,
            max_age_seconds=self.max_age_seconds,
            available=True,
            transport_ok=True,
        )


def redact_message(message: str) -> tuple[str, list[str]]:
    bounded = str(message).replace("\x00", "�")
    bounded = "".join(char if char in "\n\t" or ord(char) >= 32 else "�" for char in bounded)
    evidence: list[str] = []
    for category, pattern, replacement in _REDACTIONS:
        bounded, count = pattern.subn(replacement, bounded)
        if count:
            evidence.append(category)
    return bounded[:16_384], evidence


def _error_observation(error: LogObservationError) -> Observation[LogPage]:
    return Observation.from_sample(
        None,
        observed_at=None,
        now=datetime.now(timezone.utc),
        max_age_seconds=90,
        available=False,
        transport_ok=True,
        error=ObservationError(error.code.value, error.message),
    )


def _unavailable_observation(error: LogObservationError) -> Observation[LogPage]:
    return Observation(
        transport_ok=True,
        available=False,
        state=ObservationState.UNAVAILABLE,
        error=ObservationError(error.code.value, error.message),
        max_age_seconds=90,
    )
