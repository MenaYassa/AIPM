from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aipm.capabilities.advisor.api import create_advisor_router
from aipm.dashboard.server import create_app
from aipm.models.advisor import AdvisorResponse, AdvisorScope
from aipm.models.config import AIPMConfig, TelemetryConfig
from aipm.repositories.telemetry.read_snapshot import (
    RESOURCE_METRICS,
    SnapshotCompleteness,
    TelemetryMetricCompleteness,
    TelemetrySnapshotExport,
)
from aipm.services.advisor.composition import AdvisorCompositionRequest, compose_advisor
from aipm.services.advisor.orchestration import AdvisorOrchestrationError, AdvisorOrchestrationService


UTC = timezone.utc
EVALUATION_TIME = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def snapshot(evaluation_time: datetime = EVALUATION_TIME) -> TelemetrySnapshotExport:
    return TelemetrySnapshotExport(
        host_id="configured-host",
        evaluation_time=evaluation_time,
        window_start=evaluation_time - timedelta(minutes=5),
        window_end=evaluation_time,
        cadence_seconds=15.0,
        sample_runs=(),
        host_samples=(),
        metric_completeness=tuple(
            TelemetryMetricCompleteness(metric, SnapshotCompleteness.INSUFFICIENT, 0, 0.0, "insufficient_points")
            for metric in RESOURCE_METRICS
        ),
        completeness=SnapshotCompleteness.INSUFFICIENT,
    )


def config() -> AIPMConfig:
    return AIPMConfig(
        host_id="configured-host",
        telemetry=TelemetryConfig(database_path="/tmp/not-opened-by-fake-export.db"),
    )


def test_service_owns_deterministic_context_and_connects_export_adapter_composition() -> None:
    captured: dict[str, object] = {}
    request_response: AdvisorResponse | None = None

    def exporter(received_config, **kwargs):
        captured["config"] = received_config
        captured.update(kwargs)
        return snapshot(kwargs["evaluation_time"])

    def adapter(received_snapshot, **kwargs):
        captured["snapshot"] = received_snapshot
        captured.update({f"adapter_{key}": value for key, value in kwargs.items()})
        return AdvisorCompositionRequest(
            request_id=kwargs["request_id"],
            evaluation_time=kwargs["evaluation_time"],
            observations=(),
            scope=AdvisorScope.HOST,
            expected_sources={},
            history_envelopes=(),
        )

    def composer(request: AdvisorCompositionRequest) -> AdvisorResponse:
        nonlocal request_response
        captured["request"] = request
        request_response = compose_advisor(request)
        return request_response

    service = AdvisorOrchestrationService(
        config(),
        clock=lambda: EVALUATION_TIME,
        boundary_resolver=lambda _config, **_kwargs: EVALUATION_TIME,
        exporter=exporter,
        adapter=adapter,
        composer=composer,
    )
    response = service.evaluate()

    assert response is request_response
    assert response.evaluation_time == EVALUATION_TIME
    assert response.generated_at == EVALUATION_TIME
    assert response.request_id.startswith("advisor-live-")
    assert captured["config"] is service.config
    assert captured["evaluation_time"] == EVALUATION_TIME
    assert captured["window_start"] == EVALUATION_TIME - timedelta(minutes=5)
    assert captured["window_end"] == EVALUATION_TIME
    assert captured["adapter_evaluation_time"] == EVALUATION_TIME
    assert captured["request"].request_id == response.request_id
    assert captured["request"].scope is AdvisorScope.HOST


def test_service_aligns_clock_to_completed_telemetry_boundary_before_export():
    captured: dict[str, object] = {}

    def boundary_resolver(received_config, **kwargs):
        captured["boundary_config"] = received_config
        captured["clock_time"] = kwargs["at_or_before"]
        return EVALUATION_TIME

    def exporter(received_config, **kwargs):
        captured["export_config"] = received_config
        captured.update(kwargs)
        return snapshot(kwargs["evaluation_time"])

    service = AdvisorOrchestrationService(
        config(),
        clock=lambda: EVALUATION_TIME + timedelta(seconds=37),
        boundary_resolver=boundary_resolver,
        exporter=exporter,
    )

    response = service.evaluate()

    assert isinstance(response, AdvisorResponse)
    assert captured["boundary_config"] is service.config
    assert captured["clock_time"] == EVALUATION_TIME + timedelta(seconds=37)
    assert captured["evaluation_time"] == EVALUATION_TIME
    assert captured["window_start"] == EVALUATION_TIME - timedelta(minutes=5)
    assert captured["window_end"] == EVALUATION_TIME
    assert response.evaluation_time == EVALUATION_TIME
    assert response.generated_at == EVALUATION_TIME


def test_service_uses_real_phase4d_adapter_and_phase4a_composition_without_database_access() -> None:
    service = AdvisorOrchestrationService(
        config(),
        clock=lambda: EVALUATION_TIME,
        boundary_resolver=lambda _config, **_kwargs: EVALUATION_TIME,
        exporter=lambda _config, **kwargs: snapshot(kwargs["evaluation_time"]),
    )

    response = service.evaluate()

    assert isinstance(response, AdvisorResponse)
    assert response.evaluation_time == EVALUATION_TIME
    assert response.generated_at == EVALUATION_TIME
    assert response.scope is AdvisorScope.HOST
    assert response.available is False


def test_service_rejects_non_timezone_aware_clock_value() -> None:
    service = AdvisorOrchestrationService(
        config(),
        clock=lambda: datetime(2026, 8, 25, 12, 0),
        exporter=lambda *_args, **_kwargs: pytest.fail("export must not run"),
    )

    with pytest.raises(AdvisorOrchestrationError, match="timezone-aware"):
        service.evaluate()


def test_service_fails_closed_when_telemetry_is_disabled() -> None:
    disabled = AIPMConfig(
        host_id="configured-host",
        telemetry=TelemetryConfig(enabled=False, database_path="/tmp/not-opened-by-disabled-export.db"),
    )
    service = AdvisorOrchestrationService(
        disabled,
        clock=lambda: EVALUATION_TIME,
        exporter=lambda *_args, **_kwargs: pytest.fail("export must not run"),
    )

    with pytest.raises(AdvisorOrchestrationError, match="unavailable"):
        service.evaluate()


def test_live_get_route_returns_bounded_response_without_post_action_semantics() -> None:
    expected = compose_advisor(
        AdvisorCompositionRequest(
            request_id="route-test",
            evaluation_time=EVALUATION_TIME,
            observations=(),
            scope=AdvisorScope.HOST,
            expected_sources={},
            history_envelopes=(),
        )
    )
    app = FastAPI()
    app.include_router(create_advisor_router(orchestrator=lambda: expected))
    client = TestClient(app)

    response = client.get("/api/advisor")

    assert response.status_code == 200
    assert response.json() == expected.canonical()
    assert client.post("/api/advisor").status_code == 405


def test_live_get_route_fails_closed_without_orchestrator_or_on_safe_orchestration_error() -> None:
    app = FastAPI()
    app.include_router(create_advisor_router())
    client = TestClient(app)
    unavailable = client.get("/api/advisor")
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "error": {
            "code": "ADVISOR_UNAVAILABLE",
            "message": "Advisor evaluation is unavailable",
            "fields": [],
        }
    }

    def fail():
        raise AdvisorOrchestrationError("private telemetry path")

    app = FastAPI()
    app.include_router(create_advisor_router(orchestrator=fail))
    response = TestClient(app).get("/api/advisor")
    assert response.status_code == 503
    assert "private telemetry path" not in response.text
    assert "/tmp" not in response.text


def test_create_app_wires_server_owned_live_orchestrator_to_get_route() -> None:
    expected = compose_advisor(
        AdvisorCompositionRequest(
            request_id="server-route-test",
            evaluation_time=EVALUATION_TIME,
            observations=(),
            scope=AdvisorScope.HOST,
            expected_sources={},
            history_envelopes=(),
        )
    )
    app = create_app(advisor_orchestrator=lambda: expected)
    response = TestClient(app).get("/api/advisor")

    assert response.status_code == 200
    assert response.json() == expected.canonical()


def test_live_get_route_hides_unexpected_orchestration_exception() -> None:
    def fail():
        raise RuntimeError("SQLite password=/private/secret")

    app = FastAPI()
    app.include_router(create_advisor_router(orchestrator=fail))
    response = TestClient(app).get("/api/advisor")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "Advisor evaluation failed",
            "fields": [],
        }
    }
    assert "password" not in response.text
    assert "/private/secret" not in response.text


def test_create_app_does_not_require_application_config_for_unrelated_routes() -> None:
    class ConfigTrapApplication:
        @property
        def config(self):
            raise AssertionError("advisor configuration was accessed during unrelated app construction")

    class FakeDashboard:
        history_api = None

        def overview(self):
            return {"host": {"available": True}}

    dependency_names = (
        "incidents_api",
        "notifications_api",
        "service_health_api",
        "server_api",
        "docker_api",
        "project_api",
        "systemd_api",
        "logs_api",
        "settings_api",
    )
    kwargs = {name: object() for name in dependency_names}
    app = create_app(application=ConfigTrapApplication(), dashboard_api=FakeDashboard(), **kwargs)
    client = TestClient(app)

    assert client.get("/healthz").status_code == 200
    assert client.get("/api/overview").status_code == 200


def test_create_app_live_route_receives_configured_application_context(monkeypatch) -> None:
    import aipm.dashboard.server as dashboard_server

    expected = compose_advisor(
        AdvisorCompositionRequest(
            request_id="configured-route-test",
            evaluation_time=EVALUATION_TIME,
            observations=(),
            scope=AdvisorScope.HOST,
            expected_sources={},
            history_envelopes=(),
        )
    )
    config_marker = object()
    captured: list[object] = []

    class ConfiguredApplication:
        config = config_marker

    class FakeDashboard:
        history_api = None

        def overview(self):
            return {"host": {"available": True}}

    class FakeOrchestrationService:
        def __init__(self, received_config):
            captured.append(received_config)

        def evaluate(self):
            return expected

    monkeypatch.setattr(dashboard_server, "AdvisorOrchestrationService", FakeOrchestrationService)
    dependency_names = (
        "incidents_api",
        "notifications_api",
        "service_health_api",
        "server_api",
        "docker_api",
        "project_api",
        "systemd_api",
        "logs_api",
        "settings_api",
    )
    kwargs = {name: object() for name in dependency_names}
    app = create_app(application=ConfiguredApplication(), dashboard_api=FakeDashboard(), **kwargs)
    response = TestClient(app).get("/api/advisor")

    assert response.status_code == 200
    assert response.json() == expected.canonical()
    assert captured == [config_marker]
