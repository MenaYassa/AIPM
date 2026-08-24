"""Private authenticated HTTP transport for the MC-6.13 advisor.

This module owns only transport decoding, the authentication boundary, safe error
responses, and serialization of the existing Phase 4A response. It never collects
observations, reads a clock, or accesses runtime, provider, persistence, or authority
systems.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from aipm.models.advisor import (
    AdvisorResponse,
    AdvisorScope,
    AdvisorValidationError,
    EvidenceState,
    ProvenanceReference,
)
from aipm.services.advisor.composition import (
    AdvisorCompositionRequest,
    CompositionError,
    MAX_COMPOSITION_HISTORY_ENVELOPES,
    MAX_COMPOSITION_OBSERVATIONS,
    compose_advisor,
)
from aipm.services.advisor.normalizer import MAX_EXPECTED_SOURCES, NormalizationError
from aipm.services.advisor.rules import MAX_HISTORY_POINTS, ResourceHistoryEnvelope, ResourceHistoryPoint


ADVISOR_ROUTE: Final[str] = "/api/advisor/evaluate"
MAX_PROVENANCE_RECORDS: Final[int] = 32
MAX_SAFE_LINK_RECORDS: Final[int] = 32
_ALLOWED_REQUEST_KEYS = frozenset({"request_id", "evaluation_time", "scope", "observations", "expected_sources", "provenance", "history_envelopes"})
_ALLOWED_OBSERVATION_KEYS = frozenset({"evidence_id", "source_id", "resource_type", "resource_id", "state", "observed_at", "freshness_deadline", "max_age_seconds", "fields", "safe_links", "source_revision", "provenance_refs"})
_ALLOWED_HISTORY_ENVELOPE_KEYS = frozenset({"resource_id", "metric", "unit", "cadence_seconds", "window_start", "window_end", "complete", "points"})
_ALLOWED_HISTORY_POINT_KEYS = frozenset({"evidence_id", "observed_at", "metric", "value", "unit", "state"})
_ALLOWED_PROVENANCE_KEYS = frozenset({"provenance_ref_id", "source_id", "provenance_id", "key_id", "signature_verified", "plan_id", "plan_digest", "observed_at", "freshness_deadline"})


class AdvisorAuthenticationUnavailable(RuntimeError):
    """Raised when no approved authentication mechanism is available."""


class AdvisorAuthenticationRejected(PermissionError):
    """Raised when the caller does not satisfy the approved auth boundary."""


AdvisorAuthenticator = Callable[[Request], object | bool | None]
AdvisorComposer = Callable[[AdvisorCompositionRequest], AdvisorResponse]


def _error_payload(code: str, message: str, fields: tuple[dict[str, str], ...] = ()) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "fields": list(fields)}}


def _response(status_code: int, code: str, message: str, fields: tuple[dict[str, str], ...] = ()) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=_error_payload(code, message, fields))


def _field(path: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "code": code, "message": message}


def _parse_time(value: Any, path: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(_field(path, "timestamp_type", "timestamp must be an RFC 3339 string"))
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(_field(path, "timestamp_format", "timestamp must be a valid RFC 3339 value")) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(_field(path, "timezone_required", "timestamp must be timezone-aware"))
    return parsed


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(_field(path, "object_required", "value must be an object"))
    return value


def _reject_unknown(record: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
    if any(key not in allowed for key in record):
        raise ValueError(_field(path, "unsupported_field", "request contains an unsupported field"))


def _history_point(value: Any, envelope_index: int, point_index: int) -> ResourceHistoryPoint:
    path = f"history_envelopes[{envelope_index}].points[{point_index}]"
    record = _mapping(value, path)
    _reject_unknown(record, _ALLOWED_HISTORY_POINT_KEYS, path)
    try:
        return ResourceHistoryPoint(
            evidence_id=record["evidence_id"],
            observed_at=_parse_time(record["observed_at"], f"{path}.observed_at"),
            metric=record["metric"],
            value=record["value"],
            unit=record.get("unit", "percent"),
            state=EvidenceState(record.get("state", EvidenceState.OBSERVED.value)),
        )
    except KeyError as exc:
        raise ValueError(_field(f"{path}.{exc.args[0]}", "required", "required field is missing")) from exc
    except (AdvisorValidationError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and exc.args and isinstance(exc.args[0], dict):
            raise
        raise ValueError(_field(path, "history_invalid", "history point violates the approved bounded contract")) from exc


def _history_envelope(value: Any, index: int) -> ResourceHistoryEnvelope:
    path = f"history_envelopes[{index}]"
    record = _mapping(value, path)
    _reject_unknown(record, _ALLOWED_HISTORY_ENVELOPE_KEYS, path)
    raw_points = record.get("points")
    if not isinstance(raw_points, list):
        raise ValueError(_field(f"{path}.points", "sequence_required", "points must be an array"))
    if len(raw_points) > MAX_HISTORY_POINTS:
        raise ValueError(_field(f"{path}.points", "bound_exceeded", "points exceed the approved bound"))
    try:
        points = tuple(_history_point(point, index, point_index) for point_index, point in enumerate(raw_points))
        return ResourceHistoryEnvelope(
            resource_id=record["resource_id"],
            metric=record["metric"],
            unit=record["unit"],
            cadence_seconds=record["cadence_seconds"],
            window_start=_parse_time(record["window_start"], f"{path}.window_start"),
            window_end=_parse_time(record["window_end"], f"{path}.window_end"),
            complete=record["complete"],
            points=points,
        )
    except KeyError as exc:
        raise ValueError(_field(f"{path}.{exc.args[0]}", "required", "required field is missing")) from exc
    except (AdvisorValidationError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and exc.args and isinstance(exc.args[0], dict):
            raise
        raise ValueError(_field(path, "history_invalid", "history envelope violates the approved bounded contract")) from exc


def _provenance(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError(_field("provenance", "sequence_required", "provenance must be an array"))
    if len(value) > MAX_PROVENANCE_RECORDS:
        raise ValueError(_field("provenance", "bound_exceeded", "provenance exceeds the approved bound"))
    records: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        record = _mapping(item, f"provenance[{index}]")
        _reject_unknown(record, _ALLOWED_PROVENANCE_KEYS, f"provenance[{index}]")
        records.append(record)
    return tuple(records)


def _expected_sources(value: Any) -> list[str] | dict[str, int]:
    if isinstance(value, list):
        if len(value) > MAX_EXPECTED_SOURCES or any(not isinstance(item, str) for item in value):
            raise ValueError(_field("expected_sources", "invalid", "expected_sources must be a bounded source list"))
        return list(value)
    if isinstance(value, dict):
        if len(value) > MAX_EXPECTED_SOURCES:
            raise ValueError(_field("expected_sources", "bound_exceeded", "expected_sources exceeds the approved bound"))
        if any(not isinstance(source, str) or not isinstance(count, int) or isinstance(count, bool) for source, count in value.items()):
            raise ValueError(_field("expected_sources", "invalid", "expected_sources must contain source counts"))
        return dict(value)
    raise ValueError(_field("expected_sources", "sequence_or_object_required", "expected_sources must be an array or object"))


def _observations(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(_field("observations", "sequence_required", "observations must be an array"))
    if len(value) > MAX_COMPOSITION_OBSERVATIONS:
        raise ValueError(_field("observations", "bound_exceeded", "observations exceed the approved bound"))
    records = []
    for index, item in enumerate(value):
        record = _mapping(item, f"observations[{index}]")
        _reject_unknown(record, _ALLOWED_OBSERVATION_KEYS, f"observations[{index}]")
        records.append(record)
    return records


def _decode_payload(payload: Any) -> AdvisorCompositionRequest:
    if not isinstance(payload, dict):
        raise ValueError(_field("body", "object_required", "request body must be an object"))
    _reject_unknown(payload, _ALLOWED_REQUEST_KEYS, "body")
    required = ("request_id", "evaluation_time", "observations", "expected_sources", "history_envelopes")
    missing = next((name for name in required if name not in payload), None)
    if missing is not None:
        raise ValueError(_field(missing, "required", "required field is missing"))
    request_id = payload["request_id"]
    if not isinstance(request_id, str):
        raise ValueError(_field("request_id", "string_required", "request_id must be a string"))
    evaluation_time = _parse_time(payload["evaluation_time"], "evaluation_time")
    scope = payload.get("scope", AdvisorScope.OVERVIEW.value)
    if not isinstance(scope, str):
        raise ValueError(_field("scope", "string_required", "scope must be a string"))
    observations = _observations(payload["observations"])
    expected_sources = _expected_sources(payload["expected_sources"])
    provenance = _provenance(payload.get("provenance", []))
    raw_history = payload["history_envelopes"]
    if not isinstance(raw_history, list):
        raise ValueError(_field("history_envelopes", "sequence_required", "history_envelopes must be an array"))
    if len(raw_history) > MAX_COMPOSITION_HISTORY_ENVELOPES:
        raise ValueError(_field("history_envelopes", "bound_exceeded", "history_envelopes exceed the approved bound"))
    history = tuple(_history_envelope(item, index) for index, item in enumerate(raw_history))
    return AdvisorCompositionRequest(
        request_id=request_id,
        evaluation_time=evaluation_time,
        observations=observations,
        scope=scope,
        expected_sources=expected_sources,
        provenance=provenance,
        history_envelopes=history,
    )


class AdvisorApi:
    """Private authenticated read-only transport over the Phase 4A seam."""

    def __init__(self, *, authenticator: AdvisorAuthenticator | None = None, composer: AdvisorComposer = compose_advisor) -> None:
        self.authenticator = authenticator
        self.composer = composer

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.post(ADVISOR_ROUTE)
        async def evaluate(request: Request) -> JSONResponse:
            try:
                if self.authenticator is None:
                    raise AdvisorAuthenticationUnavailable
                principal = self.authenticator(request)
                if principal is None or principal is False:
                    raise AdvisorAuthenticationRejected
            except AdvisorAuthenticationUnavailable:
                return _response(503, "AUTHENTICATION_UNAVAILABLE", "Advisor authentication is unavailable")
            except (AdvisorAuthenticationRejected, PermissionError):
                return _response(401, "AUTHENTICATION_REQUIRED", "Advisor authentication is required")
            except Exception:
                return _response(503, "AUTHENTICATION_UNAVAILABLE", "Advisor authentication is unavailable")

            del principal
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                return _response(400, "MALFORMED_REQUEST", "Malformed advisor request body")
            try:
                payload = await request.json()
            except Exception:
                return _response(400, "MALFORMED_REQUEST", "Malformed advisor request body")
            try:
                composition_request = _decode_payload(payload)
            except ValueError as exc:
                detail = exc.args[0] if exc.args else None
                fields = (detail,) if isinstance(detail, dict) else (_field("body", "invalid", "request failed validation"),)
                return _response(422, "VALIDATION_ERROR", "Advisor request failed validation", fields)
            except (CompositionError, AdvisorValidationError, NormalizationError, TypeError):
                return _response(422, "VALIDATION_ERROR", "Advisor request failed validation", (_field("body", "invalid", "request failed validation"),))

            try:
                response = self.composer(composition_request)
                if not isinstance(response, AdvisorResponse):
                    raise TypeError("composer returned an invalid response")
                return JSONResponse(status_code=200, content=response.canonical())
            except (CompositionError, AdvisorValidationError, NormalizationError):
                return _response(422, "VALIDATION_ERROR", "Advisor request failed validation", (_field("body", "invalid", "request failed validation"),))
            except Exception:
                return _response(500, "INTERNAL_ERROR", "Advisor evaluation failed")

        return router


def create_advisor_router(*, authenticator: AdvisorAuthenticator | None = None, composer: AdvisorComposer = compose_advisor) -> APIRouter:
    """Build the isolated advisor router with an explicit fail-closed auth boundary."""

    return AdvisorApi(authenticator=authenticator, composer=composer).router()


__all__ = [
    "ADVISOR_ROUTE",
    "AdvisorApi",
    "AdvisorAuthenticationRejected",
    "AdvisorAuthenticationUnavailable",
    "AdvisorAuthenticator",
    "create_advisor_router",
]
