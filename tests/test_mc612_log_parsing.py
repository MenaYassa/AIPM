"""MC-6.12: file log provider parses Python logging timestamps (space + comma millis)."""
from __future__ import annotations

from datetime import datetime, timezone

from aipm.providers.logs import _split_timestamp


def test_split_timestamp_parses_python_logging_format():
    line = "2026-09-01 10:53:21,536 INFO dashboard ready"
    timestamp, message = _split_timestamp(line, fallback=datetime(2020, 1, 1, tzinfo=timezone.utc))

    assert timestamp == datetime(2026, 9, 1, 10, 53, 21, 536_000, tzinfo=timezone.utc)
    assert message == "INFO dashboard ready"


def test_split_timestamp_falls_back_for_non_timestamp_lines():
    fallback = datetime(2020, 1, 1, tzinfo=timezone.utc)
    timestamp, message = _split_timestamp("not a timestamp line", fallback=fallback)

    assert timestamp == fallback
    assert message == "not a timestamp line"


def test_split_timestamp_still_parses_journald_short_iso():
    timestamp, message = _split_timestamp("2026-09-01T10:53:21+00:00 INFO ok", fallback=datetime(2020, 1, 1, tzinfo=timezone.utc))

    assert timestamp == datetime(2026, 9, 1, 10, 53, 21, tzinfo=timezone.utc)
    assert message == "INFO ok"


def test_split_timestamp_rejects_short_comma_prefix():
    fallback = datetime(2020, 1, 1, tzinfo=timezone.utc)
    timestamp, message = _split_timestamp("2026-9-1 10:53:21,536 INFO ok", fallback=fallback)

    assert timestamp == fallback
