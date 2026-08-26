from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from typer.testing import CliRunner

from aipm.capabilities.dashboard.context import MissionControlContext
from aipm.cli.app import app
from aipm.tui.renderers import project_view, render_projection
from aipm.tui.session import TuiSession, VIEWS


class FakeDashboard:
    def overview(self):
        return {"available": True, "status": "fresh", "snapshot": {"state": "fresh"}, "secret_ref": "PRIVATE"}


class FakeServer:
    def server(self):
        return {"available": True, "status": "fresh", "host": {"state": "fresh"}, "health": {"status": "ok"}}


class FakeDocker:
    def summary(self, *, limit):
        assert limit == 20
        return {"available": True, "status": "fresh", "containers": [{"name": "aipm", "state": "running"}]}


class FakeProjects:
    def projects(self, *, limit):
        assert limit == 20
        return {"available": True, "status": "fresh", "projects": [{"name": "AIPM", "state": "runtime_backed"}]}


class FakeSystemd:
    def units(self, *, limit):
        assert limit == 20
        return {"available": True, "status": "fresh", "units": [{"id": "aipm-dashboard", "state": "active"}]}

    def unit(self, unit_id):
        raise AssertionError("detail is not used by the bounded first slice")


class FakeLogs:
    def logs(self, **kwargs):
        assert kwargs["limit"] == 50
        assert kwargs["max_bytes"] == 20_000
        return {"available": True, "status": "fresh", "entries": [], "sources": [], "truncated": False}


class FakeIncidents:
    def events_page(self, **kwargs):
        assert kwargs["limit"] == 20
        return {"available": True, "status": "fresh", "events": [], "has_more": False}

    def incidents_page(self, **kwargs):
        assert kwargs["limit"] == 20
        return {"available": True, "status": "fresh", "incidents": [], "has_more": False}

    def timeline(self, incident_id, *, limit, cursor=None):
        assert incident_id == 42
        assert limit == 50
        return {"available": True, "status": "fresh", "timeline": [], "has_more": False}


class FakeHistory:
    def host(self, range_name, limit):
        assert range_name == "24h"
        assert limit == 50
        return {"available": True, "status": "fresh", "points": [], "has_more": False}


class FakeNotifications:
    def __init__(self):
        self.calls = []

    def notifications(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs == {"limit": 20}
        return {
            "available": True,
            "status": "ok",
            "notifications": [
                {
                    "id": 7,
                    "status": "pending",
                    "severity": "warning",
                    "body": "DO_NOT_PRINT",
                    "destination": "DO_NOT_PRINT",
                    "secret_ref": "DO_NOT_PRINT",
                    "provider": "DO_NOT_PRINT",
                },
            ],
        }


class FakeSettings:
    def posture(self):
        return {
            "available": True,
            "status": "ok",
            "deployment": {"binding": "loopback_only_required", "public_ingress": "not_observed", "permanent_service": "not_observed"},
            "read_only": {"sqlite_mode": "read_only", "query_only": True},
            "notifications": {
                "enabled": False,
                "provider_state": "disabled",
                "audit": {"availability": "unavailable", "pending": None, "sent": None},
            },
        }


def fake_context():
    return SimpleNamespace(
        dashboard=FakeDashboard(),
        server=FakeServer(),
        docker=FakeDocker(),
        projects=FakeProjects(),
        systemd=FakeSystemd(),
        logs=FakeLogs(),
        incidents=FakeIncidents(),
        history=FakeHistory(),
        notifications=FakeNotifications(),
        settings=FakeSettings(),
    )


def console_buffer(width=100):
    stream = StringIO()
    return Console(file=stream, width=width, force_terminal=False, color_system=None), stream


def test_cli_registers_observation_only_tui_group():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "tui" in result.stdout
    tui_help = runner.invoke(app, ["tui", "--help"])
    assert tui_help.exit_code == 0
    assert "Observation-only Mission Control" in tui_help.stdout
    assert "--watch" in tui_help.stdout


def test_context_accepts_injected_facades_without_constructing_new_dependencies():
    application = SimpleNamespace()
    context = MissionControlContext.from_application(
        application,
        dashboard=object(),
        incidents=object(),
        notifications=object(),
        service_health=object(),
        server=object(),
        docker=object(),
        projects=object(),
        systemd=object(),
        logs=object(),
        settings=object(),
    )
    assert context.application is application
    assert context.dashboard is not None
    assert context.history is None


def test_each_supported_view_delegates_to_existing_facade_bounds():
    session = TuiSession(fake_context(), console=Console(file=StringIO(), force_terminal=False))
    for view in VIEWS:
        if view == "timeline":
            session.incident_id = 42
        projection = session.render_view(view)
        assert projection.title
        assert projection.status in {"fresh", "ok"}


def test_interactive_navigation_supports_selection_back_refresh_and_quit():
    output = StringIO()
    choices = iter(["2", "r", "b", "q"])
    session = TuiSession(
        fake_context(),
        console=Console(file=output, force_terminal=False),
        input_fn=lambda prompt: next(choices),
    )
    assert session.run(interactive=True) == 0
    rendered = output.getvalue()
    assert "Server" in rendered
    assert "Overview" in rendered
    assert "Choose a view number" in rendered


def test_watch_refresh_is_one_bounded_loop():
    sleeps = []
    session = TuiSession(
        fake_context(),
        console=Console(file=StringIO(), force_terminal=False),
        sleep_fn=sleeps.append,
    )
    assert session.run(view="server", watch=True, interval_seconds=5, iterations=3, interactive=False) == 0
    assert sleeps == [5, 5]


def test_invalid_view_and_refresh_bounds_fail_safely():
    session = TuiSession(fake_context(), console=Console(file=StringIO(), force_terminal=False))
    assert session.run(view="not-a-view", interactive=False) == 2
    assert session.run(view="server", watch=True, interval_seconds=0, iterations=1, interactive=False) == 2
    assert session.run(view="server", watch=True, interval_seconds=1, iterations=61, interactive=False) == 2


def test_missing_timeline_identity_is_not_observed_not_empty_success():
    session = TuiSession(fake_context(), console=Console(file=StringIO(), force_terminal=False))
    projection = session.render_view("timeline")
    assert projection.available is False
    assert projection.status == "not_observed"
    assert any(label == "Message" for label, _ in projection.lines)


def test_notifications_view_delegates_once_with_fixed_bound():
    context = fake_context()
    session = TuiSession(context, console=Console(file=StringIO(), force_terminal=False))
    projection = session.render_view("notifications")
    assert projection.title == "Notifications"
    assert projection.available is True
    assert context.notifications.calls == [{"limit": 20}]
    assert projection.has_more is False


def test_notifications_renderer_exposes_only_safe_metadata():
    projection = project_view(
        "notifications",
        {
            "available": True,
            "status": "ok",
            "notifications": [
                {
                    "id": 42,
                    "status": "failed",
                    "severity": "critical",
                    "body": "PRIVATE_BODY",
                    "destination": "PRIVATE_DESTINATION",
                    "secret_ref": "PRIVATE_SECRET",
                    "provider": "PRIVATE_PROVIDER",
                },
            ],
        },
    )
    output, stream = console_buffer()
    render_projection(output, projection)
    text = stream.getvalue()
    assert "42" in text
    assert "failed / critical" in text
    for forbidden in ("PRIVATE_BODY", "PRIVATE_DESTINATION", "PRIVATE_SECRET", "PRIVATE_PROVIDER"):
        assert forbidden not in text


def test_notifications_unavailable_response_is_rendered_safely():
    projection = project_view(
        "notifications",
        {"available": False, "status": "unavailable", "error": "Notification data unavailable", "notifications": []},
    )
    output, stream = console_buffer()
    render_projection(output, projection)
    text = stream.getvalue()
    assert projection.available is False
    assert "unavailable" in text.lower()
    assert "Notification data unavailable" in text


def test_unavailable_settings_metrics_are_not_rendered_as_zero():
    projection = project_view(
        "settings",
        {
            "available": True,
            "status": "ok",
            "notifications": {"enabled": False, "provider_state": "disabled", "audit": {"availability": "unavailable", "pending": None, "sent": None}},
        },
    )
    output, stream = console_buffer()
    render_projection(output, projection)
    text = stream.getvalue()
    assert "Audit status" in text
    assert "Unavailable" in text
    assert "Pending" in text
    assert "—" in text
    assert "Pending                 0" not in text


def test_observed_zero_settings_metric_remains_zero():
    projection = project_view(
        "settings",
        {"available": True, "status": "ok", "notifications": {"enabled": False, "audit": {"availability": "observed", "pending": 0, "sent": 0}}},
    )
    output, stream = console_buffer()
    render_projection(output, projection)
    assert "Observed" in stream.getvalue()
    assert "0" in stream.getvalue()


def test_renderer_allow_lists_and_bounds_dynamic_values():
    projection = project_view(
        "server",
        {
            "available": True,
            "status": "fresh",
            "host": {"state": "fresh"},
            "private_secret": "DO_NOT_PRINT",
            "database_path": "/private/path",
        },
    )
    output, stream = console_buffer(width=40)
    render_projection(output, projection)
    text = stream.getvalue()
    assert "DO_NOT_PRINT" not in text
    assert "/private/path" not in text
    assert len(max(text.splitlines(), key=len, default="")) <= 80


def test_tui_source_has_no_control_plane_operations():
    source = "\n".join(
        Path(path).read_text()
        for path in (
            "src/aipm/tui/renderers.py",
            "src/aipm/tui/session.py",
            "src/aipm/cli/mission_control.py",
        )
    )
    for forbidden in (
        "subprocess",
        "os.system",
        "shell=True",
        "systemctl start",
        "systemctl stop",
        "systemctl restart",
        "docker start",
        "docker stop",
        "compose up",
        "compose down",
        "git pull",
        "send_notification",
        "retry_notification",
        "reconcile_notification",
        "ChannelRegistry",
        "HttpAdapter",
        "TelegramAdapter",
        "WebSocket",
        "EventSource",
    ):
        assert forbidden not in source


def test_cli_tui_invocation_uses_shared_context_and_safe_exit(monkeypatch):
    from aipm.cli import mission_control

    monkeypatch.setattr(mission_control.MissionControlContext, "from_application", lambda application: fake_context())
    runner = CliRunner()
    result = runner.invoke(app, ["tui", "--view", "settings"])
    assert result.exit_code == 0
    assert "Settings & Notification Posture" in result.stdout


def test_cli_rejects_unknown_view_before_context_construction(monkeypatch):
    from aipm.cli import mission_control

    called = []
    monkeypatch.setattr(mission_control.MissionControlContext, "from_application", lambda application: called.append(True))
    runner = CliRunner()
    result = runner.invoke(app, ["tui", "--view", "unknown"])
    assert result.exit_code == 2
    assert called == []


def test_eof_and_keyboard_interrupt_exit_interactive_cleanly_without_traceback():
    for interruption in (EOFError(), KeyboardInterrupt()):
        output = StringIO()
        session = TuiSession(
            fake_context(),
            console=Console(file=output, force_terminal=False),
            input_fn=lambda prompt, interruption=interruption: (_ for _ in ()).throw(interruption),
        )
        assert session.run(interactive=True) == 0
        assert "Traceback" not in output.getvalue()


def test_keyboard_interrupt_exits_watch_cleanly_without_traceback():
    output = StringIO()
    session = TuiSession(
        fake_context(),
        console=Console(file=output, force_terminal=False),
        sleep_fn=lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    assert session.run(view="server", watch=True, iterations=2) == 0
    assert "Traceback" not in output.getvalue()


def test_pagination_passes_opaque_cursor_and_stops_at_bounded_page_count():
    class PagedLogs(FakeLogs):
        def __init__(self):
            self.cursors = []

        def logs(self, **kwargs):
            self.cursors.append(kwargs.get("cursor"))
            token = f"opaque-{len(self.cursors)}"
            return {"available": True, "status": "fresh", "entries": [], "sources": [], "has_more": True, "next_cursor": token}

    logs = PagedLogs()
    context = fake_context()
    context.logs = logs
    session = TuiSession(context, console=Console(file=StringIO(), force_terminal=False))
    first = session.render_view("logs")
    assert first.next_cursor == "opaque-1"
    assert session._next_page() is True
    assert logs.cursors == [None, "opaque-1"]
    assert session.page_index == 1
    for _ in range(20):
        session._next_page()
    assert len(logs.cursors) == 10
    assert session._next_page() is False


def test_missing_next_cursor_fails_safely_without_fetching_another_page():
    class MissingCursorLogs(FakeLogs):
        def __init__(self):
            self.calls = 0

        def logs(self, **kwargs):
            self.calls += 1
            return {"available": True, "status": "fresh", "entries": [], "sources": [], "has_more": True, "next_cursor": None}

    logs = MissingCursorLogs()
    context = fake_context()
    context.logs = logs
    session = TuiSession(context, console=Console(file=StringIO(), force_terminal=False))
    session.render_view("logs")
    assert session._next_page() is False
    assert logs.calls == 1


def test_pagination_source_does_not_decode_or_construct_cursor_tokens():
    source = Path("src/aipm/tui/session.py").read_text()
    assert "decode(" not in source
    assert "base64" not in source
    assert "KeysetCursor" not in source
    assert "LogCursor" not in source


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"status": "not_observed"}, False),
        ({"status": "unknown"}, False),
        ({"status": "unavailable"}, False),
        ({"status": "error"}, False),
        ({"status": "stale"}, True),
        ({"status": "fresh"}, True),
        ({"status": "never_sampled"}, True),
        ({"status": "unknown", "available": True}, True),
        ({"status": "fresh", "available": False}, False),
        ({}, False),
    ],
)
def test_availability_mapping_is_conservative(response, expected):
    assert project_view("server", response).available is expected


def test_legacy_overview_without_top_level_status_uses_nested_host_evidence():
    projection = project_view("overview", {"host": {"available": True}, "docker": {"available": False}})
    assert projection.available is True


def test_views_without_pagination_do_not_offer_next_page():
    projection = project_view("server", {"available": True, "status": "fresh", "host": {"state": "fresh"}})
    assert projection.has_more is False
    assert projection.next_cursor is None


def test_valid_long_cursor_is_preserved_exactly_without_renderer_sanitization():
    class LongCursorLogs(FakeLogs):
        def __init__(self):
            self.cursors = []

        def logs(self, **kwargs):
            self.cursors.append(kwargs.get("cursor"))
            return {
                "available": True,
                "status": "fresh",
                "entries": [],
                "sources": [],
                "has_more": kwargs.get("cursor") is None,
                "next_cursor": "A" * 200 if kwargs.get("cursor") is None else None,
            }

    logs = LongCursorLogs()
    context = fake_context()
    context.logs = logs
    session = TuiSession(context, console=Console(file=StringIO(), force_terminal=False))
    session.render_view("logs")
    assert session._next_page() is True
    assert logs.cursors[1] == "A" * 200


def test_punctuation_cursor_is_preserved_and_non_string_cursors_are_rejected():
    punctuation = "sig:v1/+/=_-.:,;?&%"
    assert project_view("logs", {"available": True, "status": "fresh", "has_more": True, "next_cursor": punctuation}).next_cursor == punctuation
    for invalid in ({}, [], 123, True, None):
        projection = project_view("logs", {"available": True, "status": "fresh", "has_more": True, "next_cursor": invalid})
        assert projection.next_cursor is None


def test_invalid_cursor_does_not_trigger_another_facade_call():
    class InvalidCursorLogs(FakeLogs):
        def __init__(self):
            self.calls = 0

        def logs(self, **kwargs):
            self.calls += 1
            return {"available": True, "status": "fresh", "entries": [], "sources": [], "has_more": True, "next_cursor": {"invalid": True}}

    logs = InvalidCursorLogs()
    context = fake_context()
    context.logs = logs
    session = TuiSession(context, console=Console(file=StringIO(), force_terminal=False))
    session.render_view("logs")
    assert session._next_page() is False
    assert logs.calls == 1


def test_dynamic_renderer_values_remain_literal_and_emit_no_osc8_markup():
    values = (
        "[link=https://evil.example]TOKEN[/link]",
        "[bold red]SECRET[/bold red]",
        "[italic]text[/italic]",
        "<script>alert(1)</script>",
        "\\x1b]8;;https://evil.example\\x1b\\\\TOKEN\\x1b]8;;\\x1b\\",
        "/home/ubuntu/.ssh/id_rsa",
        "token=SECRET",
        "exception-looking string: private traceback detail",
    )
    projection = project_view("server", {"available": True, "status": "fresh", "host": {"state": values[0]}, "error": values[1]})
    projection = projection.__class__(
        title=projection.title,
        status=projection.status,
        available=projection.available,
        lines=tuple(("dynamic", value) for value in values),
    )
    stream = StringIO()
    console = Console(file=stream, width=120, force_terminal=True, color_system="standard")
    render_projection(console, projection)
    output = stream.getvalue()
    for value in values:
        assert value in output
    assert "\x1b]8;" not in output
    assert "\x1b]8;;" not in output


def test_dynamic_markup_is_not_parsed_while_static_panel_style_remains():
    stream = StringIO()
    console = Console(file=stream, width=80, force_terminal=True, color_system="standard")
    render_projection(console, project_view("server", {"available": True, "status": "fresh", "host": {"state": "[bold]literal[/bold]"}}))
    output = stream.getvalue()
    assert "[bold]literal[/bold]" in output
    assert "Test" not in output
    assert "\x1b[" in output
