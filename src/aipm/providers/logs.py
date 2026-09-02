from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence

from aipm.models.logs import LogQuery, LogSeverity, LogSource, ProviderLogLine


class LogProviderError(RuntimeError):
    """Internal adapter failure; never serialized directly."""


class LogProvider(Protocol):
    def read(self, source: LogSource, query: LogQuery) -> Sequence[ProviderLogLine]: ...


class JournaldLogProvider:
    def __init__(self, *, runner=subprocess.run, timeout_seconds: float = 10.0, max_output_bytes: int = 1_000_000) -> None:
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def read(self, source: LogSource, query: LogQuery) -> Sequence[ProviderLogLine]:
        if source.unit_id is None or source.kind.value != "journald":
            raise LogProviderError("unsupported journal source")
        args = [
            "journalctl",
            "--no-pager",
            "--output=short-iso",
            "--utc",
            f"--since={query.since.isoformat()}",
            f"--until={query.until.isoformat()}",
            "-u",
            source.source_ref,
        ]
        try:
            result = self.runner(
                args,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LogProviderError("journal source unavailable") from exc
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        if len(stdout.encode("utf-8", "replace")) > self.max_output_bytes:
            stdout = stdout.encode("utf-8", "replace")[: self.max_output_bytes].decode("utf-8", "ignore")
        if result.returncode != 0:
            raise LogProviderError("journal source unavailable")
        return _parse_lines(stdout, default_unit=source.unit_id)


class FixedFileLogProvider:
    def __init__(self, *, max_read_bytes: int = 1_000_000) -> None:
        self.max_read_bytes = max_read_bytes

    def read(self, source: LogSource, query: LogQuery) -> Sequence[ProviderLogLine]:
        if source.kind.value != "file":
            raise LogProviderError("unsupported file source")
        path = Path(source.source_ref)
        try:
            raw = path.read_bytes()[-self.max_read_bytes :]
        except (OSError, ValueError) as exc:
            raise LogProviderError("file source unavailable") from exc
        return _parse_lines(raw.decode("utf-8", "replace"), default_unit=None)


def _parse_lines(text: str, *, default_unit: str | None) -> list[ProviderLogLine]:
    now = datetime.now(timezone.utc)
    parsed: list[ProviderLogLine] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        timestamp, message = _split_timestamp(line, fallback=now)
        severity = _infer_severity(message)
        parsed.append(ProviderLogLine(timestamp, severity, message[:16_384], unit_id=default_unit))
    return parsed


def _split_timestamp(line: str, *, fallback: datetime) -> tuple[datetime, str]:
    candidate = line[:32].strip()
    for length in (25, 24):
        try:
            parsed = datetime.fromisoformat(candidate[:length].replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc), line[length:].lstrip()
        except ValueError:
            continue
    # Python logging format: "2026-09-01 10:53:21,536 LEVEL message" (19-char prefix + ",mmm").
    try:
        parsed = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        millis = line[20:23]
        if line[19] == "," and millis.isdigit() and len(line) > 23 and line[23] == " ":
            parsed = parsed.replace(microsecond=int(millis) * 1000, tzinfo=timezone.utc)
            return parsed, line[24:]
    except ValueError:
        pass
    return fallback, line


def _infer_severity(message: str) -> LogSeverity:
    lowered = message.lower()
    if any(token in lowered for token in ("critical", "fatal", "panic")):
        return LogSeverity.CRITICAL
    if any(token in lowered for token in ("error", "exception", "traceback")):
        return LogSeverity.ERROR
    if any(token in lowered for token in ("warn", "deprecated")):
        return LogSeverity.WARNING
    if "notice" in lowered:
        return LogSeverity.NOTICE
    if any(token in lowered for token in ("debug", "trace")):
        return LogSeverity.DEBUG
    return LogSeverity.INFO
