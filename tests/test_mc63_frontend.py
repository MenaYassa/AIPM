from pathlib import Path


STATIC_DIR = Path(__file__).parents[1] / "src" / "aipm" / "dashboard" / "static"
HTML = STATIC_DIR / "index.html"


def test_server_view_contains_all_read_only_sections_and_contract_route() -> None:
    text = HTML.read_text(encoding="utf-8")
    for marker in (
        "Server & Host Intelligence",
        "Identity",
        "CPU & Load",
        "Memory & Swap",
        "Disk",
        "Network",
        "Health",
        "Host History",
        "/api/server",
        "/api/history/host",
        "serverObservationState",
        "serverObservationAge",
    ):
        assert marker in text


def test_server_view_does_not_fabricate_deferred_history_or_warning_data() -> None:
    text = HTML.read_text(encoding="utf-8")
    assert "RX/TX and per-filesystem history are not fabricated." in text
    assert "Resource warning projection is not available from current telemetry." in text
    assert "Filesystem detail unavailable from current telemetry." in text
    assert "Interface detail unavailable from current telemetry." in text
    assert "fake metric" not in text.lower()


def test_server_uses_central_scheduler_at_30_seconds_and_history_at_60() -> None:
    text = HTML.read_text(encoding="utf-8")
    assert "scheduler.register('server',loadServer,{intervalMs:30000})" in text
    assert "scheduler.register('server-history',loadServerHistory,{intervalMs:60000})" in text
    assert "scheduler.refresh('server-history')" in text
    assert "setInterval(" not in text


def test_server_state_and_responsive_markers_are_present() -> None:
    text = HTML.read_text(encoding="utf-8")
    for state in ("fresh", "stale", "unavailable", "never_sampled", "unknown", "error"):
        assert state in text
    assert ".server-grid" in text
    assert ".server-card-wide" in text
    assert "@media(max-width:640px)" in text
    assert "@media(max-width:820px)" in text
    assert "mission-control-state.js" in text
    assert "mission-control-scheduler.js" in text


def test_server_frontend_has_no_mutation_or_secret_surface() -> None:
    text = text_lower = HTML.read_text(encoding="utf-8").lower()
    assert "/api/server" in text_lower
    assert 'method="post"' not in text_lower
    assert 'method="put"' not in text_lower
    assert 'method="patch"' not in text_lower
    assert 'method="delete"' not in text_lower
    assert "/acknowledge" not in text_lower
    assert "websocket" not in text_lower
    assert "eventsource" not in text_lower
