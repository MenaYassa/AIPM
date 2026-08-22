"""Bounded MC-6.12B AF_UNIX protocol.

The ordinary caller channel exposes observation/provenance requests only. It has
no approval, consume, or authoritative audit append operation.
"""
from __future__ import annotations

import base64
import json
import re
import struct
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = "mc612b-provenance-v1"
MAX_FRAME = 256 * 1024
MAX_REQUEST_FRAME = 16 * 1024
_ID = re.compile(r"^[0-9a-f]{32}$")
_PROVENANCE_ID = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SAFE_IDEMPOTENCY = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")


def canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def encode_frame(value: dict[str, Any], *, limit: int = MAX_FRAME) -> bytes:
    payload = canonical_json(value)
    if len(payload) > limit:
        raise ValueError("protocol frame exceeds bound")
    return struct.pack("!I", len(payload)) + payload


def decode_frame(data: bytes, *, limit: int = MAX_FRAME) -> dict[str, Any]:
    if len(data) < 4:
        raise ValueError("truncated frame")
    size = struct.unpack("!I", data[:4])[0]
    if size > limit or len(data) != size + 4:
        raise ValueError("invalid frame length")
    value = json.loads(data[4:].decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("frame must be an object")
    return value


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(value: str, *, length: int) -> bytes:
    if not isinstance(value, str) or "=" in value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid base64url")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(raw) != length:
        raise ValueError("invalid decoded length")
    return raw


def _require_keys(value: dict[str, Any], required: set[str], allowed: set[str]) -> None:
    if set(value) != allowed or not required.issubset(value):
        raise ValueError("invalid protocol fields")


@dataclass(frozen=True, slots=True)
class ObservationRequest:
    request_id: str
    nonce: str
    operation: str
    target_id: str
    idempotency_key: str
    source_id: str = "host"

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.request_id) or not _ID.fullmatch(self.nonce):
            raise ValueError("invalid request identifier")
        if not _SAFE_IDEMPOTENCY.fullmatch(self.idempotency_key):
            raise ValueError("invalid idempotency key")
        if self.operation != "update_project_plan" or self.source_id != "host":
            raise ValueError("unsupported observation request")
        if not self.target_id or len(self.target_id) > 128 or any(ord(c) < 32 for c in self.target_id):
            raise ValueError("invalid target")

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": self.request_id,
            "nonce": self.nonce,
            "operation": self.operation,
            "target_id": self.target_id,
            "idempotency_key": self.idempotency_key,
            "source_id": self.source_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ObservationRequest":
        allowed = {"protocol_version", "request_id", "nonce", "operation", "target_id", "idempotency_key", "source_id"}
        _require_keys(value, allowed, allowed)
        if value["protocol_version"] != PROTOCOL_VERSION:
            raise ValueError("unsupported protocol version")
        return cls(value["request_id"], value["nonce"], value["operation"], value["target_id"], value["idempotency_key"], value["source_id"])


@dataclass(frozen=True, slots=True)
class ProvenanceResponse:
    request: ObservationRequest
    service_instance_id: str
    provenance_id: str
    plan_payload: dict[str, Any]
    plan_id: str
    digest: str
    observed_at: str
    freshness_deadline: str
    created_at: str
    expires_at: str
    evidence_state: str
    evidence_source: str
    evidence: list[list[str]]
    key_id: str
    signature: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "key_id": self.key_id,
            "request_id": self.request.request_id,
            "nonce": self.request.nonce,
            "service_instance_id": self.service_instance_id,
            "provenance_id": self.provenance_id,
            "operation": self.request.operation,
            "target_id": self.request.target_id,
            "idempotency_key": self.request.idempotency_key,
            "source_id": self.request.source_id,
            "plan_payload": self.plan_payload,
            "plan_id": self.plan_id,
            "digest": self.digest,
            "observed_at": self.observed_at,
            "freshness_deadline": self.freshness_deadline,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "evidence_state": self.evidence_state,
            "evidence_source": self.evidence_source,
            "evidence": self.evidence,
        }

    def signed_dict(self) -> dict[str, Any]:
        result = self.unsigned_dict()
        result["signature"] = self.signature
        return result

    def signing_bytes(self) -> bytes:
        return b"aipm-mc612b-provenance-v1\x00" + canonical_json(self.unsigned_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProvenanceResponse":
        allowed = {
            "protocol_version", "key_id", "request_id", "nonce", "service_instance_id",
            "provenance_id", "operation", "target_id", "idempotency_key", "source_id",
            "plan_payload", "plan_id", "digest", "observed_at", "freshness_deadline",
            "created_at", "expires_at", "evidence_state", "evidence_source", "evidence", "signature",
        }
        _require_keys(value, allowed, allowed)
        if value["protocol_version"] != PROTOCOL_VERSION or not _KEY_ID.fullmatch(value["key_id"]):
            raise ValueError("invalid signed response identity")
        request = ObservationRequest(
            value["request_id"], value["nonce"], value["operation"], value["target_id"],
            value["idempotency_key"], value["source_id"],
        )
        if not _PROVENANCE_ID.fullmatch(value["provenance_id"]):
            raise ValueError("invalid provenance ID")
        if not isinstance(value["plan_payload"], dict) or not isinstance(value["evidence"], list):
            raise ValueError("invalid signed response payload")
        b64url_decode(value["signature"], length=64)
        if not isinstance(value["plan_id"], str) or len(value["plan_id"]) != 32:
            raise ValueError("invalid plan ID")
        if not isinstance(value["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["digest"]):
            raise ValueError("invalid digest")
        return cls(
            request=request,
            service_instance_id=value["service_instance_id"],
            provenance_id=value["provenance_id"],
            plan_payload=value["plan_payload"],
            plan_id=value["plan_id"],
            digest=value["digest"],
            observed_at=value["observed_at"],
            freshness_deadline=value["freshness_deadline"],
            created_at=value["created_at"],
            expires_at=value["expires_at"],
            evidence_state=value["evidence_state"],
            evidence_source=value["evidence_source"],
            evidence=value["evidence"],
            key_id=value["key_id"],
            signature=value["signature"],
        )

    def verify_signature(self, public_key: Any) -> None:
        from aipm.provenance.crypto import verify
        verify(public_key, b64url_decode(self.signature, length=64), self.signing_bytes())
