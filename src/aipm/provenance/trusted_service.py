"""MC-6.12B trusted process core.

This process is the future provenance authority. The ordinary socket protocol
exposes observation/provenance requests only; approval, consume, and authoritative
audit append are internal concepts and have no caller RPC.
"""
from __future__ import annotations

import os
import secrets
import socket
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from aipm.control_plane.identity import EXPECTED_EFFECT, PLAN_IDENTITY_VERSION, plan_id, plan_digest
from aipm.control_plane.models import (
    ActionPlan,
    ActionRequest,
    EvidenceSource,
    EvidenceState,
    EvidenceSummary,
    OperationKind,
    PlanState,
    RiskLevel,
    PLAN_TTL,
)
from aipm.provenance.adapters import HostAdapter
from aipm.provenance.crypto import load_private_key, sign, verify
from aipm.provenance.protocol import (
    MAX_REQUEST_FRAME,
    ObservationRequest,
    ProvenanceResponse,
    b64url_decode,
    b64url_encode,
    decode_frame,
    encode_frame,
)


@dataclass(frozen=True, slots=True)
class ReplayEntry:
    fingerprint: str
    provenance_id: str
    expires_at: datetime


class TrustedProvenanceService:
    """Trusted-process-owned observation/provenance authority."""

    def __init__(self, *, key_path: str | Path, key_id: str, target_allow_list: set[str] | frozenset[str], clock: Callable[[], datetime] | None = None, allowed_uids: set[int] | frozenset[int] = frozenset(), allowed_gids: set[int] | frozenset[int] = frozenset(), max_replay: int = 4096):
        if not target_allow_list or not key_id:
            raise ValueError("trusted service requires explicit configuration")
        if max_replay < 1 or max_replay > 4096:
            raise ValueError("invalid replay bound")
        if not allowed_uids and not allowed_gids:
            raise ValueError("peer allow-list is required")
        self._key = load_private_key(key_path)
        self._key_id = key_id
        self._targets = frozenset(target_allow_list)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._allowed_uids = frozenset(allowed_uids)
        self._allowed_gids = frozenset(allowed_gids)
        self._instance_id = secrets.token_hex(16)
        self._replay: dict[str, ReplayEntry] = {}
        self._max_replay = max_replay
        self._host = HostAdapter()

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    def observe(self, request: ObservationRequest) -> ProvenanceResponse:
        if request.target_id not in self._targets:
            raise ValueError("target is not allow-listed")
        now = self._now()
        fingerprint = request.request_id + ":" + request.nonce + ":" + request.target_id + ":" + request.idempotency_key
        existing = self._replay.get(request.request_id)
        if existing is not None and existing.expires_at > now:
            if existing.fingerprint != fingerprint:
                raise ValueError("request replay mismatch")
            raise ValueError("request replay")
        if len(self._replay) >= self._max_replay:
            self._replay = {key: item for key, item in self._replay.items() if item.expires_at > now}
            if len(self._replay) >= self._max_replay:
                raise ValueError("replay bound reached")
        action_request = ActionRequest(
            operation=OperationKind.UPDATE_PROJECT_PLAN,
            target_id=request.target_id,
            idempotency_key=request.idempotency_key,
        )
        observation = self._host.observe(now=now)
        if observation.state is not EvidenceState.OBSERVED:
            raise ValueError("observation unavailable")
        evidence = observation.evidence
        expires_at = now + PLAN_TTL
        pid = plan_id(
            request=action_request,
            evidence=evidence,
            evidence_source=EvidenceSource.MISSION_CONTROL_OBSERVATION,
            risk=RiskLevel.LOW,
            expected_effect=EXPECTED_EFFECT,
            created_at=now,
            expires_at=expires_at,
            state=PlanState.PLANNED,
        )
        draft = ActionPlan(
            plan_id=pid,
            request=action_request,
            risk=RiskLevel.LOW,
            evidence=evidence,
            evidence_source=EvidenceSource.MISSION_CONTROL_OBSERVATION,
            expected_effect=EXPECTED_EFFECT,
            expires_at=expires_at,
            created_at=now,
            state=PlanState.PLANNED,
        )
        digest = plan_digest(draft)
        plan = ActionPlan(
            plan_id=pid,
            request=action_request,
            risk=RiskLevel.LOW,
            evidence=evidence,
            evidence_source=EvidenceSource.MISSION_CONTROL_OBSERVATION,
            expected_effect=EXPECTED_EFFECT,
            expires_at=expires_at,
            created_at=now,
            state=PlanState.PLANNED,
            digest=digest,
        )
        provenance_id = secrets.token_hex(32)
        response = ProvenanceResponse(
            request=request,
            service_instance_id=self._instance_id,
            provenance_id=provenance_id,
            plan_payload=plan.canonical_payload(),
            plan_id=plan.plan_id,
            digest=plan.digest,
            observed_at=observation.observed_at.isoformat(),
            freshness_deadline=observation.freshness_deadline.isoformat(),
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
            evidence_state=EvidenceState.OBSERVED.value,
            evidence_source=EvidenceSource.MISSION_CONTROL_OBSERVATION.value,
            evidence=evidence.canonical(),
            key_id=self._key_id,
        )
        signature = b64url_encode(sign(self._key, response.signing_bytes()))
        self._replay[request.request_id] = ReplayEntry(fingerprint, provenance_id, expires_at)
        return ProvenanceResponse(
            request=response.request,
            service_instance_id=response.service_instance_id,
            provenance_id=response.provenance_id,
            plan_payload=response.plan_payload,
            plan_id=response.plan_id,
            digest=response.digest,
            observed_at=response.observed_at,
            freshness_deadline=response.freshness_deadline,
            created_at=response.created_at,
            expires_at=response.expires_at,
            evidence_state=response.evidence_state,
            evidence_source=response.evidence_source,
            evidence=response.evidence,
            key_id=response.key_id,
            signature=signature,
        )

    def accept_internal(self, response: ProvenanceResponse) -> None:
        """Trusted-process-only acceptance hook; never exposed by the caller protocol."""
        if response.service_instance_id != self._instance_id or response.key_id != self._key_id:
            raise ValueError("invalid service instance")
        signature = b64url_decode(response.signature, length=64)
        verify(self._key.public_key(), signature, response.signing_bytes())
        if response.evidence_state != EvidenceState.OBSERVED.value or response.evidence_source != EvidenceSource.MISSION_CONTROL_OBSERVATION.value:
            raise ValueError("invalid trusted evidence")
        if response.plan_payload.get("plan_id") != response.plan_id:
            raise ValueError("plan identity mismatch")
        if response.plan_payload.get("request", {}).get("target_id") != response.request.target_id:
            raise ValueError("target binding mismatch")
        if response.plan_payload.get("evidence_state") != response.evidence_state:
            raise ValueError("evidence binding mismatch")
        if response.plan_payload.get("evidence_source") != response.evidence_source:
            raise ValueError("source binding mismatch")

    def check_peer(self, conn: socket.socket) -> None:
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("peer credentials unavailable")
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, gid = struct.unpack("3i", raw)
        if self._allowed_uids and uid not in self._allowed_uids:
            if not self._allowed_gids or gid not in self._allowed_gids:
                raise PermissionError("unauthorized peer")

    def handle_frame(self, frame: bytes) -> bytes:
        request = ObservationRequest.from_dict(decode_frame(frame, limit=MAX_REQUEST_FRAME))
        return encode_frame(self.observe(request).signed_dict())

    def serve_socket(self, listener: socket.socket) -> None:
        while True:
            conn, _addr = listener.accept()
            with conn:
                self.check_peer(conn)
                data = conn.recv(MAX_REQUEST_FRAME + 4)
                conn.sendall(self.handle_frame(data))


def socket_activated_listener(fd: int = 3) -> socket.socket:
    listener = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
    listener.set_inheritable(False)
    return listener


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--socket-activation", action="store_true")
    args, extra = parser.parse_known_args()
    if extra or not args.socket_activation:
        raise SystemExit("socket activation is required")
    key_path = os.environ.get("AIPM_PROVENANCE_KEY_PATH", "/etc/aipm/provenance/provenance-ed25519.key")
    key_id = os.environ.get("AIPM_PROVENANCE_KEY_ID", "prov-ed25519-v1")
    targets = frozenset(filter(None, os.environ.get("AIPM_PROVENANCE_TARGETS", "").split(",")))
    if not targets:
        raise SystemExit("trusted target allow-list is required")
    allowed_uids = frozenset(int(value) for value in filter(None, os.environ.get("AIPM_PROVENANCE_ALLOWED_UIDS", "").split(",")))
    allowed_gids = frozenset(int(value) for value in filter(None, os.environ.get("AIPM_PROVENANCE_ALLOWED_GIDS", "").split(",")))
    service = TrustedProvenanceService(
        key_path=key_path,
        key_id=key_id,
        target_allow_list=targets,
        allowed_uids=allowed_uids,
        allowed_gids=allowed_gids,
    )
    service.serve_socket(socket_activated_listener())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
