from __future__ import annotations

from pathlib import Path


STATIC_DIR = Path(__file__).parents[1] / "src" / "aipm" / "dashboard" / "static"
HTML = STATIC_DIR / "index.html"

ROUTES = (
    "dashboard",
    "server",
    "docker",
    "projects",
    "systemd",
    "logs",
    "incidents",
    "history",
    "notifications",
    "settings",
    "ai-agent",
)


def test_application_shell_contains_all_navigation_entries_and_safe_hashes() -> None:
    text = HTML.read_text(encoding="utf-8")
    assert 'class="app-shell"' in text
    assert 'id="sidebar"' in text
    assert 'id="sidebarToggle"' in text
    assert 'id="pageTitle"' in text
    assert 'id="pageSubtitle"' in text
    for route in ROUTES:
        assert f'href="#/{route}"' in text
        assert f'data-route="{route}"' in text
        assert f'data-view="{route}"' in text


def test_only_dashboard_is_visible_by_default_and_placeholders_have_no_fake_metrics() -> None:
    text = HTML.read_text(encoding="utf-8")
    assert 'data-view="dashboard"' in text
    assert 'data-view="server" hidden' in text
    assert 'data-view="docker" hidden' in text
    assert 'data-view="projects" hidden' in text
    assert 'data-view="systemd" hidden' in text
    assert 'data-view="logs" hidden' in text
    assert 'data-view="incidents" hidden' in text
    assert 'data-view="history" hidden' in text
    assert 'data-view="notifications" hidden' in text
    assert 'data-view="settings" hidden' in text
    assert 'data-view="ai-agent" hidden' in text
    assert text.count("Coming in MC-6.x") >= 10
    assert "No new server data source is queried in MC-6.2." in text
    assert "No Docker action or new provider is introduced in MC-6.2." in text
    assert "No new history store or telemetry implementation is introduced." in text
    assert "Notifications remain disabled" in text
    assert "fake metric" not in text.lower()


def test_dashboard_content_and_existing_api_routes_remain_present() -> None:
    text = HTML.read_text(encoding="utf-8")
    for marker in (
        "System Overview",
        "Service Pulse",
        "Docker / Containers",
        "Resource History",
        "Project constellation",
        "MC-3 Event Stream",
        "Incident Room",
        "Notification Safety",
        "Handbook routes",
        "/api/overview",
        "/api/services",
        "/api/history/host",
        "/api/events?range=24h&limit=50",
        "/api/incidents?range=7d&status=open&limit=50",
        "/api/notifications?limit=50",
    ):
        assert marker in text


def test_shell_uses_mc61_state_scheduler_and_preserves_cadence() -> None:
    text = HTML.read_text(encoding="utf-8")
    assert "./mission-control-state.js" in text
    assert "./mission-control-scheduler.js" in text
    assert "./mission-control-shell.js" in text
    assert "scheduler.register('overview',()=>load(true),{intervalMs:15000})" in text
    assert "scheduler.register('services',loadServices,{intervalMs:15000})" in text
    assert "scheduler.register('events',loadEvents,{intervalMs:15000})" in text
    assert "scheduler.register('history',loadHistory,{intervalMs:60000})" in text
    assert "scheduler.register('incidents',loadIncidents,{intervalMs:30000})" in text
    assert "scheduler.register('notifications',loadNotifications,{intervalMs:30000})" in text
    assert "setInterval(" not in text
    scheduler = (STATIC_DIR / "mission-control-scheduler.js").read_text(encoding="utf-8")
    assert "visibilitychange" in scheduler
    assert "pagehide" in text


def test_shell_has_no_action_endpoint_or_mutation_reference() -> None:
    text = HTML.read_text(encoding="utf-8").lower()
    assert 'method="post"' not in text
    assert 'method="put"' not in text
    assert 'method="patch"' not in text
    assert 'method="delete"' not in text
    assert "/acknowledge" not in text
    assert "websocket" not in text
    assert "eventsource" not in text
