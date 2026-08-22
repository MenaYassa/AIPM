from __future__ import annotations

import pytest

from aipm.provenance.protocol import (
    MAX_REQUEST_FRAME,
    ObservationRequest,
    b64url_decode,
    b64url_encode,
    canonical_json,
    decode_frame,
    encode_frame,
)


def _request() -> ObservationRequest:
    return ObservationRequest(
        request_id="a" * 32,
        nonce="b" * 32,
        operation="update_project_plan",
        target_id="project-demo",
        idempotency_key="c" * 32,
        source_id="host",
    )


def test_observation_protocol_has_no_privileged_rpc_field() -> None:
    payload = _request().as_dict()
    assert set(payload) == {"protocol_version", "request_id", "nonce", "operation", "target_id", "idempotency_key", "source_id"}
    assert "approve" not in payload
    assert "consume" not in payload
    assert "audit_append" not in payload


def test_canonical_encoding_is_stable_and_exact() -> None:
    assert canonical_json({"b": 2, "a": "é"}) == '{"a":"é","b":2}'.encode("utf-8")
    assert decode_frame(encode_frame(_request().as_dict(), limit=MAX_REQUEST_FRAME)) == _request().as_dict()


def test_unpadded_base64url_is_exactly_64_bytes() -> None:
    raw = bytes(range(64))
    encoded = b64url_encode(raw)
    assert "=" not in encoded
    assert b64url_decode(encoded, length=64) == raw
    with pytest.raises(ValueError):
        b64url_decode(encoded + "=", length=64)


def test_protocol_rejects_unknown_fields_and_wrong_ids() -> None:
    value = _request().as_dict()
    value["approval_rpc"] = True
    with pytest.raises(ValueError):
        ObservationRequest.from_dict(value)
    value = _request().as_dict()
    value["request_id"] = "not-an-id"
    with pytest.raises(ValueError):
        ObservationRequest.from_dict(value)


def test_protocol_rejects_oversized_frame() -> None:
    with pytest.raises(ValueError):
        encode_frame({"x": "a" * (MAX_REQUEST_FRAME + 1)}, limit=MAX_REQUEST_FRAME)
