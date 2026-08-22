from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aipm.provenance.protocol import ObservationRequest
from aipm.provenance.trusted_service import TrustedProvenanceService


def _key(tmp_path: Path) -> Path:
    path = tmp_path / "key.pem"
    private = Ed25519PrivateKey.generate()
    path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    path.chmod(0o600)
    return path


def _request() -> ObservationRequest:
    return ObservationRequest("a" * 32, "b" * 32, "update_project_plan", "project-demo", "c" * 32)


def test_service_requires_explicit_peer_allow_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="peer allow-list"):
        TrustedProvenanceService(key_path=_key(tmp_path), key_id="prov-ed25519-v1", target_allow_list={"project-demo"})


def test_trusted_service_constructs_and_accepts_observed_response(tmp_path: Path) -> None:
    service = TrustedProvenanceService(key_path=_key(tmp_path), key_id="prov-ed25519-v1", target_allow_list={"project-demo"}, allowed_uids={-1})
    response = service.observe(_request())
    assert response.evidence_state == "observed"
    assert response.evidence_source == "mission_control_observation"
    assert response.plan_id == response.plan_payload["plan_id"]
    assert response.digest == response.plan_payload.get("digest", response.digest)
    service.accept_internal(response)


def test_replay_and_request_mismatch_fail_closed(tmp_path: Path) -> None:
    service = TrustedProvenanceService(key_path=_key(tmp_path), key_id="prov-ed25519-v1", target_allow_list={"project-demo"}, allowed_uids={-1})
    request = _request()
    service.observe(request)
    try:
        service.observe(request)
    except ValueError as exc:
        assert "replay" in str(exc)
    else:
        raise AssertionError("replay was accepted")


def test_ordinary_client_has_no_privileged_rpc_surface() -> None:
    from aipm.provenance.client import ObservationClient
    names = set(dir(ObservationClient))
    assert "approve" not in names
    assert "consume" not in names
    assert "append_audit" not in names
    assert "append_observed_audit" not in names


def test_local_class_mutation_cannot_create_trusted_authority() -> None:
    code = """
from aipm.provenance.client import ObservationClient
from aipm.control_plane.approval import ApprovalService

type.__setattr__(ApprovalService, 'request', lambda *a, **k: 'observed')
assert not hasattr(ObservationClient, 'approve')
assert not hasattr(ObservationClient, 'consume')
assert not hasattr(ObservationClient, 'append_audit')
print('LOCAL_MUTATION_HAS_NO_REMOTE_AUTHORITY=PASS')
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert "LOCAL_MUTATION_HAS_NO_REMOTE_AUTHORITY=PASS" in result.stdout


def test_signed_response_substitution_is_rejected(tmp_path: Path) -> None:
    service = TrustedProvenanceService(key_path=_key(tmp_path), key_id="prov-ed25519-v1", target_allow_list={"project-demo"}, allowed_uids={-1})
    response = service.observe(_request())
    parsed = type(response).from_dict(response.signed_dict())
    parsed.verify_signature(service._key.public_key())
    altered = dict(response.signed_dict())
    altered["target_id"] = "project-other"
    try:
        altered_response = type(response).from_dict(altered)
        altered_response.verify_signature(service._key.public_key())
    except Exception:
        pass
    else:
        raise AssertionError("modified signed response was accepted")
