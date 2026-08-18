from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aipm.capabilities.dashboard.query_bounds import (
    MAX_CURSOR_LENGTH,
    MAX_FILTER_LENGTH,
    MAX_LOG_BYTES,
    MAX_LOG_LINES,
    bounded_query,
    validate_cursor,
    validate_filter,
    validate_limit,
    validate_log_bytes,
    validate_log_lines,
    validate_offset,
    validate_range_name,
)
from aipm.capabilities.dashboard.safety import assert_safe_payload, scan_payload
from aipm.models.mission_control import Observation, ObservationError, ObservationState


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def test_observation_contract_distinguishes_transport_availability_and_freshness() -> None:
    fresh = Observation.from_sample("ok", observed_at=NOW - timedelta(seconds=10), now=NOW, max_age_seconds=45)
    stale = Observation.from_sample("old", observed_at=NOW - timedelta(seconds=46), now=NOW, max_age_seconds=45)
    never_sampled = Observation.from_sample(None, observed_at=None, now=NOW, max_age_seconds=45)
    unavailable = Observation.from_sample(None, observed_at=None, now=NOW, max_age_seconds=45, available=False)
    error = Observation.from_sample(
        None,
        observed_at=None,
        now=NOW,
        max_age_seconds=45,
        transport_ok=False,
        error=ObservationError("transport", "source unavailable"),
    )

    assert (fresh.transport_ok, fresh.available, fresh.state, fresh.data) == (True, True, ObservationState.FRESH, "ok")
    assert stale.state is ObservationState.STALE
    assert never_sampled.state is ObservationState.NEVER_SAMPLED
    assert unavailable.state is ObservationState.UNAVAILABLE
    assert (error.transport_ok, error.available, error.state) == (False, False, ObservationState.ERROR)


def test_semantic_error_is_error_even_when_transport_succeeds() -> None:
    observation = Observation.from_sample(
        None,
        observed_at=None,
        now=NOW,
        max_age_seconds=45,
        transport_ok=True,
        available=False,
        error=ObservationError("invalid_sample", "sample did not satisfy the domain contract"),
    )

    assert observation.transport_ok is True
    assert observation.available is False
    assert observation.state is ObservationState.ERROR
    assert observation.error is not None


def test_observation_contract_rejects_naive_timestamps_and_supports_unknown() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Observation.from_sample("bad", observed_at=NOW.replace(tzinfo=None), now=NOW, max_age_seconds=45)

    unknown = Observation.unknown(error=ObservationError("ambiguous", "state cannot be determined"))
    assert unknown.transport_ok is True
    assert unknown.available is False
    assert unknown.state is ObservationState.UNKNOWN
    assert unknown.error is not None


def test_bounded_query_contract_normalizes_supported_values() -> None:
    query = bounded_query(range_name="1h", limit=25, offset=4, cursor="cursor-1")
    assert query.range_name == "1h"
    assert query.range_seconds == 3600
    assert query.limit == 25
    assert query.offset == 4
    assert query.cursor == "cursor-1"

    assert validate_range_name("7d") == "7d"
    assert validate_limit(1) == 1
    assert validate_offset(0) == 0
    assert validate_cursor("") is None
    assert validate_filter("") is None


@pytest.mark.parametrize(
    ("function", "value"),
    [
        (validate_range_name, "30d"),
        (validate_limit, 0),
        (validate_limit, 5001),
        (validate_offset, -1),
        (validate_cursor, "x" * (MAX_CURSOR_LENGTH + 1)),
        (validate_filter, "x" * (MAX_FILTER_LENGTH + 1)),
        (validate_log_lines, 0),
        (validate_log_lines, MAX_LOG_LINES + 1),
        (validate_log_bytes, 0),
        (validate_log_bytes, MAX_LOG_BYTES + 1),
    ],
)
def test_bounded_query_contract_rejects_invalid_or_excessive_values(function, value) -> None:
    with pytest.raises(ValueError):
        function(value)


def test_bounded_query_contract_rejects_boolean_limits() -> None:
    with pytest.raises(ValueError):
        validate_limit(True)
    with pytest.raises(ValueError):
        validate_offset(False)


def test_safe_payload_scanner_allows_loopback_urls_and_safe_metadata() -> None:
    payload = {
        "available": True,
        "status": "ok",
        "state": "fresh",
        "link": "http://127.0.0.1:8787/healthz",
        "configured": False,
        "channels": [],
    }
    assert scan_payload(payload) == ()
    assert_safe_payload(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"token": "redacted"},
        {"provider_value": "https://example.invalid/provider"},
        {"body": "-----BEGIN PRIVATE KEY-----"},
        {"destination": "redacted"},
    ],
)
def test_safe_payload_scanner_rejects_secret_like_or_external_material(payload) -> None:
    assert scan_payload(payload)
    with pytest.raises(ValueError, match="unsafe Mission Control payload"):
        assert_safe_payload(payload)


def test_mc5_routes_remain_get_only_and_acknowledgement_is_not_exposed() -> None:
    server = Path(__file__).parents[1] / "src" / "aipm" / "dashboard" / "server.py"
    source = server.read_text(encoding="utf-8")
    assert "@app.get(\"/api/overview\")" in source
    assert "@app.get(\"/api/services\")" in source
    assert "@app.post" not in source
    assert "@app.put" not in source
    assert "@app.patch" not in source
    assert "@app.delete" not in source
    assert "/acknowledge" not in source


def test_frontend_uses_foundation_modules_without_changing_mc5_cadence_or_routes() -> None:
    static_dir = Path(__file__).parents[1] / "src" / "aipm" / "dashboard" / "static"
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    state = (static_dir / "mission-control-state.js").read_text(encoding="utf-8")
    scheduler = (static_dir / "mission-control-scheduler.js").read_text(encoding="utf-8")

    assert "/static/mission-control-state.js" in html
    assert "/static/mission-control-scheduler.js" in html
    assert "scheduler.register('overview',()=>load(true),{intervalMs:15000})" in html
    assert "scheduler.register('services',loadServices,{intervalMs:15000})" in html
    assert "scheduler.register('events',loadEvents,{intervalMs:15000})" in html
    assert "scheduler.register('history',loadHistory,{intervalMs:60000})" in html
    assert "scheduler.register('incidents',loadIncidents,{intervalMs:30000})" in html
    assert "scheduler.register('notifications',loadNotifications,{intervalMs:30000})" in html
    assert "fetch('/api/overview'" in html
    assert "/api/history/host" in html
    assert "/api/services" in html
    assert "/api/events" in html
    assert "/api/incidents" in html
    assert "/api/notifications" in html
    assert "setInterval(" not in html
    assert 'method="post"' not in html.lower()
    assert "/acknowledge" not in html.lower()
    assert "normalizeObservationState" in state
    assert "one timer per resource" not in scheduler.lower()  # implementation is behavior, not UI text
    assert "resource already registered" in scheduler
    assert "resource.running" in scheduler
    assert "visibilitychange" in scheduler
    assert "cleanup()" in scheduler
