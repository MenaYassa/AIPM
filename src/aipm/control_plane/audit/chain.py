"""Hash-chain primitives for the durable audit ledger.

Chain definition (version ``mc612-audit-v1``):

* the genesis record's ``previous_hash`` is the deterministic constant
  ``GENESIS_PREVIOUS_HASH`` — timestamps are never chain identity;
* every record's canonical payload (``canonical.py`` rules) includes its
  ``sequence``, ``event_id``, logical content, and ``previous_hash``;
* ``event_hash = SHA256(previous_hash_utf8 || 0x0A || canonical_payload_utf8)``;
* ``event_id`` is a cryptographically random opaque identifier assigned once
  at event creation; uniqueness per occurrence matters, and tamper-evidence
  comes from the chain (the id is covered by the canonical payload hash);
* sequence is the durable ordering authority, never a timestamp.
"""
from __future__ import annotations

import hashlib

from aipm.control_plane.audit.canonical import AUDIT_CHAIN_VERSION, canonical_audit_payload

GENESIS_PREVIOUS_HASH = hashlib.sha256(f"{AUDIT_CHAIN_VERSION}:genesis".encode("utf-8")).hexdigest()


def compute_event_hash(previous_hash: str, chain_payload: dict) -> str:
    """Compute the chain hash over the previous hash and canonical payload."""

    if not isinstance(previous_hash, str) or len(previous_hash) != 64:
        raise ValueError("Invalid previous hash")
    canonical = canonical_audit_payload(chain_payload)
    digest = hashlib.sha256(previous_hash.encode("utf-8") + b"\n" + canonical.encode("utf-8")).hexdigest()
    return digest
