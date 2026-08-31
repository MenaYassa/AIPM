"""Canonical serialization for the control-plane audit ledger.

Format version: ``mc612-audit-v1``.

Rules (stable across restarts, independent of Python object representation):
* the payload is a flat JSON object with lexicographically sorted keys;
* separators are ``,`` and ``:`` with no whitespace; encoding is UTF-8;
* ``None`` fields are ABSENT keys (explicit null semantics: absent, not null);
* enums serialize as their ``str`` enum value;
* integers serialize as JSON integers;
* booleans are never used in the ledger payload;
* no ``repr``, no memory addresses, no locale or dictionary-order dependence.

The chain hash input is defined in ``chain.py`` as
``SHA256(previous_hash_utf8 || 0x0A || canonical_payload_utf8)``.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

AUDIT_CHAIN_VERSION = "mc612-audit-v1"

#: Canonical field order is defined by sorted keys; this tuple documents the
#: complete closed field set and is verified by tests.
CANONICAL_FIELDS = (
    "action_id",
    "actor_role",
    "actor_subject",
    "chain_version",
    "confirmation_id",
    "decision_id",
    "environment",
    "event_hash",
    "event_id",
    "event_type",
    "lifecycle_from",
    "lifecycle_to",
    "lease_id",
    "occurred_at",
    "operation",
    "parent_event_id",
    "plan_digest",
    "plan_id",
    "plan_revision",
    "policy_version",
    "previous_hash",
    "reason",
    "result_code",
    "sequence",
    "target_id",
)


def canonical_audit_payload(payload: Mapping[str, Any]) -> str:
    """Serialize the ledger payload canonically; None values are omitted."""

    cleaned = {key: value for key, value in payload.items() if value is not None}
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return canonical_audit_payload(payload).encode("utf-8")
