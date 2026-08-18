from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from aipm.capabilities.dashboard.query_bounds import (
    MAX_CURSOR_LENGTH,
    validate_cursor,
    validate_filter,
    validate_log_bytes,
    validate_log_lines,
)


class LogSourceKind(StrEnum):
    JOURNALD = "journald"
    FILE = "file"


class LogSeverity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class LogOwner(StrEnum):
    SYSTEMD = "systemd"
    SERVER = "server"


class LogErrorCode(StrEnum):
    UNKNOWN_SOURCE = "UNKNOWN_LOG_SOURCE"
    INVALID_QUERY = "INVALID_LOG_QUERY"
    SOURCE_UNAVAILABLE = "LOG_SOURCE_UNAVAILABLE"
    SOURCE_FAILED = "LOG_SOURCE_FAILED"
    INVALID_CURSOR = "INVALID_LOG_CURSOR"


@dataclass(frozen=True, slots=True)
class LogObservationError:
    code: LogErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class LogSource:
    id: str
    label: str
    kind: LogSourceKind
    owner: LogOwner
    source_ref: str = field(repr=False)
    unit_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ProviderLogLine:
    observed_at: datetime
    severity: LogSeverity
    message: str
    unit_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class LogEntry:
    timestamp: datetime
    severity: LogSeverity
    message: str
    redacted: bool = False
    evidence: tuple[str, ...] = ()
    unit_id: str | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class LogCursor:
    source_id: str
    offset: int
    fingerprint: str

    _KEY = b"aipm-mission-control-log-cursor-v1"

    def encode(self) -> str:
        payload = {"source": self.source_id, "offset": self.offset, "fingerprint": self.fingerprint}
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(self._KEY, body, hashlib.sha256).hexdigest()[:24].encode()
        return base64.urlsafe_b64encode(body + b"." + signature).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "LogCursor":
        try:
            checked = validate_cursor(value)
            if checked is None or len(checked) > MAX_CURSOR_LENGTH:
                raise ValueError
            padded = checked + "=" * (-len(checked) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode())
            body, signature = decoded.rsplit(b".", 1)
            expected = hmac.new(cls._KEY, body, hashlib.sha256).hexdigest()[:24].encode()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(body.decode())
            source = payload["source"]
            offset = payload["offset"]
            fingerprint = payload["fingerprint"]
            if not isinstance(source, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", source):
                raise ValueError
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise ValueError
            if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{16,64}", fingerprint):
                raise ValueError
            return cls(source, offset, fingerprint)
        except Exception as exc:
            raise ValueError("invalid cursor") from exc


@dataclass(frozen=True, slots=True)
class LogQuery:
    source_id: str
    since: datetime
    until: datetime
    severity: LogSeverity | None = None
    unit_id: str | None = None
    project_id: str | None = None
    limit: int = 200
    max_bytes: int = 100_000
    cursor: LogCursor | None = None

    @classmethod
    def build(
        cls,
        *,
        source_id: str,
        since: str | None = None,
        until: str | None = None,
        severity: str | None = None,
        unit_id: str | None = None,
        project_id: str | None = None,
        limit: int = 200,
        max_bytes: int = 100_000,
        cursor: str | None = None,
        now: datetime | None = None,
    ) -> "LogQuery":
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", source_id):
            raise ValueError("source must be a backend-owned symbolic ID")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        end = _parse_timestamp(until, default=current)
        start = _parse_timestamp(since, default=end - timedelta(hours=24))
        if start > end or end - start > timedelta(days=7):
            raise ValueError("time range is outside the supported bounds")
        selected_severity = None
        if severity is not None:
            try:
                selected_severity = LogSeverity(severity.lower())
            except (AttributeError, ValueError) as exc:
                raise ValueError("severity is not allow-listed") from exc
        safe_unit = validate_filter(unit_id)
        safe_project = validate_filter(project_id)
        return cls(
            source_id=source_id,
            since=start,
            until=end,
            severity=selected_severity,
            unit_id=safe_unit,
            project_id=safe_project,
            limit=validate_log_lines(limit),
            max_bytes=validate_log_bytes(max_bytes),
            cursor=LogCursor.decode(cursor) if cursor else None,
        )

    def fingerprint(self) -> str:
        material = "|".join(
            [self.source_id, self.since.isoformat(), self.until.isoformat(), self.severity or "", self.unit_id or "", self.project_id or "", str(self.limit), str(self.max_bytes)]
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class LogPage:
    source: LogSource
    entries: tuple[LogEntry, ...]
    next_cursor: str | None = None
    truncated: bool = False
    returned_lines: int = 0
    returned_bytes: int = 0
    errors: tuple[LogObservationError, ...] = ()


def _parse_timestamp(value: str | None, *, default: datetime) -> datetime:
    if value is None or value == "":
        return default
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("timestamp is outside the supported bounds")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def source_registry(*, aipm_log_path: str) -> dict[str, LogSource]:
    sources: dict[str, LogSource] = {}
    for source_id, label, unit_id in (
        ("aipm-dashboard", "AIPM Dashboard", "aipm-dashboard"),
        ("aipm-telemetry", "AIPM Telemetry", "aipm-telemetry"),
        ("aipm-events", "AIPM Events", "aipm-events"),
        ("freebuff-llm-proxy", "Freebuff LLM Proxy", "freebuff-llm-proxy"),
        ("fastsd-webui", "FastSD WebUI", "fastsd-webui"),
        ("fastsd-webserver", "FastSD Webserver", "fastsd-webserver"),
        ("fastsd-proxy", "FastSD Proxy", "fastsd-proxy"),
    ):
        sources[source_id] = LogSource(source_id, label, LogSourceKind.JOURNALD, LogOwner.SYSTEMD, unit_id + ".service", unit_id)
    sources["aipm-file"] = LogSource("aipm-file", "AIPM Application Log", LogSourceKind.FILE, LogOwner.SERVER, aipm_log_path)
    return sources
