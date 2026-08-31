"""Central audit sanitization.

This is the ONE sanitization boundary for every control-plane audit producer:
free-text audit fields are scanned here at event-construction time, so no
producer can accidentally persist secret material. Secret-like values cause a
bounded rejection (fail closed) rather than a silent redaction, because a
redacted security record could silently lose evidence.
"""
from __future__ import annotations

MAX_AUDIT_REASON = 256
MAX_AUDIT_RESULT_CODE = 64
MAX_AUDIT_ID = 128
MAX_AUDIT_SUBJECT = 128

_SECRET_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "cookie",
    "csrf",
    "bearer ",
    "authorization:",
    "argon2",
    "$argon2",
    "credential",
    "private key",
    "-----begin",
    "api_key",
    "apikey",
    "session_id=",
    "sessionid=",
)


class AuditEventError(ValueError):
    """Raised when an audit event would violate the bounded, secret-free contract."""


def assert_no_secret_material(value: str, *, field: str) -> str:
    """Reject any value carrying secret-like material; fail closed."""

    if not isinstance(value, str):
        raise AuditEventError(f"Invalid audit {field}")
    lowered = value.casefold()
    for marker in _SECRET_MARKERS:
        if marker in lowered:
            raise AuditEventError(f"Unsafe audit {field}")
    return value


def bounded_reason(value: str, *, field: str = "reason", maximum: int = MAX_AUDIT_REASON) -> str:
    """Bound and sanitize a free-text audit reason."""

    if not isinstance(value, str):
        raise AuditEventError(f"Invalid audit {field}")
    if len(value) > maximum:
        raise AuditEventError(f"Audit {field} exceeds its bound")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AuditEventError(f"Invalid audit {field}")
    return assert_no_secret_material(value, field=field)


def bounded_code(value: str, *, field: str = "result code") -> str:
    """Bound and sanitize a result code."""

    if not isinstance(value, str) or not value or len(value) > MAX_AUDIT_RESULT_CODE:
        raise AuditEventError(f"Invalid audit {field}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AuditEventError(f"Invalid audit {field}")
    return assert_no_secret_material(value, field=field)


def bounded_reference(value: str | None, *, field: str, maximum: int = MAX_AUDIT_ID) -> str | None:
    """Bound an optional identifier reference; None means absent."""

    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AuditEventError(f"Invalid audit {field}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AuditEventError(f"Invalid audit {field}")
    return assert_no_secret_material(value, field=field)


def bounded_subject(value: str, *, field: str = "actor subject") -> str:
    """Bound and sanitize an actor subject."""

    if not isinstance(value, str) or not value or len(value) > MAX_AUDIT_SUBJECT:
        raise AuditEventError(f"Invalid audit {field}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AuditEventError(f"Invalid audit {field}")
    return assert_no_secret_material(value, field=field)
