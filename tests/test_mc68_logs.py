from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from aipm.capabilities.dashboard.logs_api import DashboardLogsApi
from aipm.capabilities.dashboard.query_bounds import MAX_LOG_BYTES, MAX_LOG_LINES
from aipm.dashboard.server import create_app
from aipm.mappers.logs import LogsResponseMapper
from aipm.models.logs import (
    LogCursor,
    LogEntry,
    LogErrorCode,
    LogQuery,
    LogSeverity,
    ProviderLogLine,
    source_registry,
)
from aipm.models.mission_control import ObservationState
from aipm.providers.logs import FixedFileLogProvider, JournaldLogProvider, LogProviderError
from aipm.services.logs.observation import ReadOnlyLogService


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


class FakeProvider:
    def __init__(self, lines=None, error: Exception | None = None):
        self.lines = lines or []
        self.error = error
        self.calls = []

    def read(self, source, query):
        self.calls.append((source.id, query.source_id))
        if self.error:
            raise self.error
        return self.lines


def make_service(lines=None, *, error=None):
    registry = source_registry(aipm_log_path="/tmp/aipm-test.log")
    provider = FakeProvider(lines, error=error)
    service = ReadOnlyLogService(registry, {"journald": provider, "file": provider}, now=lambda: NOW)
    return service, provider


def make_line(message, *, severity=LogSeverity.INFO, offset=0, unit="aipm-dashboard"):
    return ProviderLogLine(NOW.replace(hour=11, minute=offset, second=0), severity, message, unit_id=unit)


def test_log_contract_bounds_and_cursor_integrity():
    query = LogQuery.build(source_id="aipm-dashboard", now=NOW, limit=MAX_LOG_LINES, max_bytes=MAX_LOG_BYTES)
    assert query.limit == MAX_LOG_LINES
    assert query.max_bytes == MAX_LOG_BYTES
    cursor = LogCursor("aipm-dashboard", 4, query.fingerprint())
    encoded = cursor.encode()
    assert LogCursor.decode(encoded) == cursor
    tampered = encoded[:-1] + ("A" if encoded[-1] != "A" else "B")
    try:
        LogCursor.decode(tampered)
    except ValueError:
        pass
    else:
        raise AssertionError("tampered cursor accepted")


def test_unknown_source_fails_closed_before_provider_access():
    service, provider = make_service([make_line("not used")])
    query = LogQuery.build(source_id="unknown-source", now=NOW)
    observation = service.read(query)
    assert observation.state == ObservationState.ERROR
    assert observation.error is not None
    assert observation.error.code == LogErrorCode.UNKNOWN_SOURCE.value
    assert provider.calls == []


def test_redaction_happens_before_mapping_and_serialization():
    secret = "password=hunter2 Authorization: Bearer supertoken API_KEY=abc https://private.example.test/hook /home/ubuntu/secret"
    service, _ = make_service([make_line(secret)])
    query = LogQuery.build(source_id="aipm-dashboard", now=NOW)
    response = LogsResponseMapper().response(service.read(query))
    encoded = json.dumps(response)
    assert "hunter2" not in encoded
    assert "supertoken" not in encoded
    assert "abc" not in encoded
    assert "private.example.test" not in encoded
    assert "/home/ubuntu/secret" not in encoded
    assert response["entries"][0]["redacted"] is True
    assert "credential" in response["entries"][0]["evidence"]


def test_source_failure_isolated_and_safe():
    service, _ = make_service(error=LogProviderError("private provider payload"))
    query = LogQuery.build(source_id="aipm-dashboard", now=NOW)
    observation = service.read(query)
    response = LogsResponseMapper().response(observation)
    assert observation.state == ObservationState.UNAVAILABLE
    assert response["errors"][0]["message"] == "Log source is unavailable"
    assert "private provider payload" not in json.dumps(response)


def test_bounded_lines_bytes_and_next_cursor():
    lines = [make_line(f"line-{index}-" + "x" * 20, offset=index) for index in range(4)]
    service, _ = make_service(lines)
    query = LogQuery.build(source_id="aipm-dashboard", now=NOW, limit=2, max_bytes=10_000)
    page = service.read(query).data
    assert page is not None
    assert len(page.entries) == 2
    assert page.truncated is True
    assert page.next_cursor
    next_query = LogQuery.build(source_id="aipm-dashboard", now=NOW, limit=2, max_bytes=10_000, cursor=page.next_cursor)
    next_page = service.read(next_query).data
    assert next_page is not None
    assert next_page.entries[0].message.startswith("line-2")


def test_invalid_cursor_binding_fails_closed():
    service, _ = make_service([make_line("one")])
    query = LogQuery.build(source_id="aipm-dashboard", now=NOW, limit=1)
    cursor = LogCursor("aipm-dashboard", 1, "0" * 16).encode()
    invalid = LogQuery.build(source_id="aipm-dashboard", now=NOW, limit=1, cursor=cursor)
    response = LogsResponseMapper().response(service.read(invalid))
    assert response["observation"]["state"] == "error"
    assert response["errors"][0]["code"] == LogErrorCode.INVALID_CURSOR.value


def test_journald_provider_uses_fixed_safe_arguments():
    calls = []

    class Result:
        returncode = 0
        stdout = "2026-08-18T12:00:00+00:00 host aipm: warning example"
        stderr = ""

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    source = source_registry(aipm_log_path="/tmp/aipm.log")["aipm-dashboard"]
    query = LogQuery.build(source_id=source.id, now=NOW)
    lines = JournaldLogProvider(runner=runner).read(source, query)
    assert lines[0].severity == LogSeverity.WARNING
    args, kwargs = calls[0]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 10.0
    assert "--no-pager" in args
    assert "-u" in args
    assert source.source_ref in args
    assert "start" not in args and "restart" not in args


def test_fixed_file_provider_reads_only_backend_owned_path(tmp_path: Path):
    path = tmp_path / "aipm.log"
    path.write_text("2026-08-18T12:00:00+00:00 INFO safe\n", encoding="utf-8")
    source = source_registry(aipm_log_path=str(path))["aipm-file"]
    query = LogQuery.build(source_id=source.id, now=NOW)
    lines = FixedFileLogProvider().read(source, query)
    assert len(lines) == 1
    assert lines[0].message == "INFO safe"


def test_api_bounds_unknown_source_and_safe_source_projection():
    service, _ = make_service([make_line("safe")])
    api = DashboardLogsApi(service)
    response = api.logs(source="aipm-dashboard", limit=1)
    assert response["source"]["id"] == "aipm-dashboard"
    assert "source_ref" not in json.dumps(response)
    assert len(response["sources"]) == 8
    invalid = api.logs(source="../../etc/passwd")
    assert invalid["observation"]["state"] == "error"
    assert invalid["errors"][0]["code"] == "INVALID_LOG_QUERY"
    invalid_unit = api.logs(source="aipm-dashboard", unit="arbitrary-unit")
    assert invalid_unit["errors"][0]["code"] == "INVALID_LOG_QUERY"


def test_api_route_is_get_only_and_additive():
    service, _ = make_service([make_line("safe")])
    api = DashboardLogsApi(service)
    client = TestClient(create_app(logs_api=api))
    response = client.get("/api/logs?source=aipm-dashboard&limit=1")
    assert response.status_code == 200
    assert response.json()["entries"][0]["message"] == "safe"
    assert client.post("/api/logs").status_code == 405
    assert client.get("/api/logs?source=not-allow-listed").json()["observation"]["state"] == "error"
    assert client.get("/static/mission-control-logs.js").status_code == 200


def test_logs_frontend_uses_static_module_and_one_scheduler():
    html = Path("src/aipm/dashboard/static/index.html").read_text(encoding="utf-8")
    module = Path("src/aipm/dashboard/static/mission-control-logs.js").read_text(encoding="utf-8")
    assert "data-view=\"logs\"" in html
    assert "from '/static/mission-control-logs.js'" in html
    assert "scheduler.register('logs',logsController.load,{intervalMs:60000})" in html
    assert html.count("scheduler.register('logs'") == 1
    assert "download=" not in html.lower()
    assert "window.location" not in module
    assert "innerHTML" in module and "escapeHtml" in module
    assert "fetch(`/api/logs?" in module
