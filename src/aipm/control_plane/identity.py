"""Versioned MC-6.12A canonical identity helpers.

This module is a pure serialization/conformance surface. It is not an authority,
producer, registry, or local observed-evidence acceptance path. MC-6.12A local
services remain evidence-neutral; MC-6.12B's trusted process owns authority.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

PLAN_IDENTITY_VERSION = "mc612a-plan-v1"
EXPECTED_EFFECT = "No runtime effect; produce a bounded future-operation plan only"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def request_identity(request: Any) -> str:
    return hashlib.sha256(request.canonical().encode("utf-8")).hexdigest()


def plan_id_payload(*, request: Any, evidence: Any, evidence_source: Any, risk: Any,
                    expected_effect: str, created_at: datetime, expires_at: datetime,
                    state: Any) -> dict[str, Any]:
    return {
        "request_identity": request_identity(request),
        "operation": request.operation.value,
        "target_id": request.target_id,
        "evidence_source": evidence_source.value,
        "evidence_state": evidence.state.value,
        "evidence": evidence.canonical(),
        "risk": risk.value,
        "expected_effect": expected_effect,
        "created_at": _utc(created_at).isoformat(),
        "expires_at": _utc(expires_at).isoformat(),
        "state": state.value,
    }


def plan_id(*, request: Any, evidence: Any, evidence_source: Any, risk: Any,
            expected_effect: str, created_at: datetime, expires_at: datetime,
            state: Any) -> str:
    return hashlib.sha256(_canonical_json(plan_id_payload(
        request=request, evidence=evidence, evidence_source=evidence_source,
        risk=risk, expected_effect=expected_effect, created_at=created_at,
        expires_at=expires_at, state=state,
    )).encode("utf-8")).hexdigest()[:32]


def canonical_plan_payload(plan: Any) -> dict[str, Any]:
    return plan.canonical_payload()


def canonical_plan_bytes(plan: Any) -> bytes:
    return _canonical_json(canonical_plan_payload(plan)).encode("utf-8")


def plan_digest(plan: Any) -> str:
    return hashlib.sha256(canonical_plan_bytes(plan)).hexdigest()


def identity_vector(plan: Any) -> dict[str, str]:
    return {
        "version": PLAN_IDENTITY_VERSION,
        "request_identity": request_identity(plan.request),
        "plan_id": plan.plan_id,
        "digest": plan_digest(plan),
    }
