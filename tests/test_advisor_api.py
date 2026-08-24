from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aipm.capabilities.advisor.api import ADVISOR_ROUTE, AdvisorApi, create_advisor_router
from aipm.models.advisor import AdvisorResponse, AdvisorValidationError
from aipm.services.advisor.composition import AdvisorCompositionRequest, CompositionError, compose_advisor
from aipm.services.advisor.normalizer import NormalizationError


EVALUATION_TIME = "2026-08-24T12:00:00+00:00"


def payload() -> dict:
    return {
        "request_id": "api-test-request",
        "evaluation_time": EVALUATION_TIME,
        "scope": "overview",
        "observations": [
            {
                "evidence_id": "host-cpu-1",
                "source_id": "host",
                "resource_type": "host",
                "resource_id": "host",
                "state": "observed",
                "observed_at": "2026-08-24T11:59:55+00:00",
                "freshness_deadline": "2026-08-24T12:01:00+00:00",
                "fields": {"metric": "cpu_percent", "value": 72.5, "unit": "percent"},
                "safe_links": [],
                "source_revision": None,
                "provenance_refs": [],
            }
        ],
        "expected_sources": {"host": 1},
        "provenance": [],
        "history_envelopes": [],
    }


def client(*, authenticator=None, composer=compose_advisor) -> TestClient:
    app = FastAPI()
    app.include_router(create_advisor_router(authenticator=authenticator, composer=composer))
    return TestClient(app)


def assert_error(response, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    assert response.json() == {
        "error": {
            "code": code,
            "message": {
                "AUTHENTICATION_UNAVAILABLE": "Advisor authentication is unavailable",
                "AUTHENTICATION_REQUIRED": "Advisor authentication is required",
                "MALFORMED_REQUEST": "Malformed advisor request body",
                "VALIDATION_ERROR": "Advisor request failed validation",
                "INTERNAL_ERROR": "Advisor evaluation failed",
            }[code],
            "fields": [] if status_code != 422 else response.json()["error"]["fields"],
        }
    }


def test_route_constant_and_authentication_unavailable_fail_closed() -> None:
    response = client().post(ADVISOR_ROUTE, json=payload())
    assert_error(response, 503, "AUTHENTICATION_UNAVAILABLE")


def test_invalid_authentication_is_rejected_before_request_evaluation() -> None:
    called = False

    def reject(_request):
        nonlocal called
        called = True
        return False

    response = client(authenticator=reject).post(ADVISOR_ROUTE, json=payload())
    assert_error(response, 401, "AUTHENTICATION_REQUIRED")
    assert called is True


def test_authentication_exception_fails_closed_without_leaking_details() -> None:
    def unavailable(_request):
        raise RuntimeError("secret bearer token /home/ubuntu/private")

    response = client(authenticator=unavailable).post(ADVISOR_ROUTE, json=payload())
    assert_error(response, 503, "AUTHENTICATION_UNAVAILABLE")
    assert "secret" not in response.text
    assert "/home/ubuntu" not in response.text


def test_permission_denial_is_rejected_as_invalid_authentication() -> None:
    def reject(_request):
        raise PermissionError("internal authorization detail")

    response = client(authenticator=reject).post(ADVISOR_ROUTE, json=payload())
    assert_error(response, 401, "AUTHENTICATION_REQUIRED")
    assert "internal authorization detail" not in response.text


def test_non_json_request_returns_safe_400() -> None:
    response = client(authenticator=lambda _request: object()).post(
        ADVISOR_ROUTE,
        content="not-json",
        headers={"content-type": "text/plain"},
    )
    assert_error(response, 400, "MALFORMED_REQUEST")


def test_malformed_request_returns_safe_400() -> None:
    response = client(authenticator=lambda _request: object()).post(
        ADVISOR_ROUTE,
        content="{not-json",
        headers={"content-type": "application/json"},
    )
    assert_error(response, 400, "MALFORMED_REQUEST")


def test_validation_failure_returns_safe_422_without_raw_value() -> None:
    invalid = payload()
    invalid.pop("evaluation_time")
    invalid["provider_secret"] = "do-not-echo"
    response = client(authenticator=lambda _request: object()).post(ADVISOR_ROUTE, json=invalid)
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Advisor request failed validation",
            "fields": [{"path": "body", "code": "unsupported_field", "message": "request contains an unsupported field"}],
        }
    }
    assert "do-not-echo" not in response.text


def test_naive_evaluation_time_is_a_safe_validation_failure() -> None:
    invalid = payload()
    invalid["evaluation_time"] = "2026-08-24T12:00:00"
    response = client(authenticator=lambda _request: object()).post(ADVISOR_ROUTE, json=invalid)
    assert response.status_code == 422
    assert response.json()["error"]["fields"] == [
        {"path": "evaluation_time", "code": "timezone_required", "message": "timestamp must be timezone-aware"}
    ]


@pytest.mark.parametrize(
    "domain_error",
    [
        CompositionError("composition contract detail"),
        AdvisorValidationError("advisor contract detail"),
        NormalizationError("normalization contract detail"),
    ],
)
def test_composer_domain_validation_failure_returns_safe_422(domain_error: Exception) -> None:
    def fail(_request: AdvisorCompositionRequest):
        raise domain_error

    response = client(authenticator=lambda _request: object(), composer=fail).post(ADVISOR_ROUTE, json=payload())
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Advisor request failed validation",
            "fields": [{"path": "body", "code": "invalid", "message": "request failed validation"}],
        }
    }
    assert "contract detail" not in response.text


def test_internal_failure_returns_safe_500_without_exception_leakage() -> None:
    def fail(_request: AdvisorCompositionRequest):
        raise RuntimeError("SQL password=/home/ubuntu/private traceback")

    response = client(authenticator=lambda _request: object(), composer=fail).post(ADVISOR_ROUTE, json=payload())
    assert_error(response, 500, "INTERNAL_ERROR")
    assert "password" not in response.text
    assert "traceback" not in response.text
    assert "/home/ubuntu" not in response.text


def test_identical_requests_produce_identical_advisor_response() -> None:
    api_client = client(authenticator=lambda _request: object())
    first = api_client.post(ADVISOR_ROUTE, json=payload())
    second = api_client.post(ADVISOR_ROUTE, json=payload())
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["evaluation_time"] == EVALUATION_TIME
    assert first.json()["generated_at"] == EVALUATION_TIME


def test_evaluation_time_is_preserved_at_composition_boundary() -> None:
    captured: list[AdvisorCompositionRequest] = []

    def capture(request: AdvisorCompositionRequest) -> AdvisorResponse:
        captured.append(request)
        return compose_advisor(request)

    response = client(authenticator=lambda _request: object(), composer=capture).post(ADVISOR_ROUTE, json=payload())
    assert response.status_code == 200
    assert len(captured) == 1
    assert captured[0].evaluation_time == datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    assert response.json()["evaluation_time"] == EVALUATION_TIME
    assert response.json()["generated_at"] == EVALUATION_TIME


def test_history_envelope_is_reconstructed_as_typed_phase3_input() -> None:
    request_payload = payload()
    request_payload["history_envelopes"] = [
        {
            "resource_id": "container-a",
            "metric": "cpu_percent",
            "unit": "percent",
            "cadence_seconds": 15,
            "window_start": "2026-08-24T11:55:00+00:00",
            "window_end": "2026-08-24T12:00:00+00:00",
            "complete": True,
            "points": [
                {
                    "evidence_id": "history-a-1",
                    "observed_at": "2026-08-24T11:55:00+00:00",
                    "metric": "cpu_percent",
                    "value": 86,
                    "unit": "percent",
                    "state": "observed",
                },
                {
                    "evidence_id": "history-a-2",
                    "observed_at": "2026-08-24T11:57:30+00:00",
                    "metric": "cpu_percent",
                    "value": 87,
                    "unit": "percent",
                    "state": "observed",
                },
                {
                    "evidence_id": "history-a-3",
                    "observed_at": "2026-08-24T12:00:00+00:00",
                    "metric": "cpu_percent",
                    "value": 88,
                    "unit": "percent",
                    "state": "observed",
                },
            ],
        }
    ]
    captured: list[AdvisorCompositionRequest] = []

    def capture(request: AdvisorCompositionRequest) -> AdvisorResponse:
        captured.append(request)
        return compose_advisor(request)

    response = client(authenticator=lambda _request: object(), composer=capture).post(ADVISOR_ROUTE, json=request_payload)
    assert response.status_code == 200
    assert len(captured) == 1
    assert len(captured[0].history_envelopes) == 1
    envelope = captured[0].history_envelopes[0]
    assert envelope.resource_id == "container-a"
    assert [point.evidence_id for point in envelope.points] == ["history-a-1", "history-a-2", "history-a-3"]
    assert envelope.points[0].observed_at.tzinfo is not None


def test_response_is_the_existing_advisor_response_serialization_without_wrapper() -> None:
    expected: list[AdvisorResponse] = []

    def capture(request: AdvisorCompositionRequest) -> AdvisorResponse:
        response = compose_advisor(request)
        expected.append(response)
        return response

    response = client(authenticator=lambda _request: object(), composer=capture).post(ADVISOR_ROUTE, json=payload())
    assert response.status_code == 200
    assert response.json() == expected[0].canonical()
    assert set(response.json()) == set(expected[0].canonical())
    assert "transport_ok" not in response.json()
    assert "http_status" not in response.json()


def test_unsupported_nested_fields_are_rejected_and_no_runtime_sources_are_imported() -> None:
    invalid = payload()
    invalid["observations"][0]["filesystem_path"] = "/etc/shadow"
    response = client(authenticator=lambda _request: object()).post(ADVISOR_ROUTE, json=invalid)
    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["path"] == "observations[0]"

    source = open("src/aipm/capabilities/advisor/api.py", encoding="utf-8").read()
    assert "datetime.now" not in source
    assert "import sqlite" not in source
    assert "import docker" not in source
    assert "subprocess" not in source
