"""Update approval security contract.

This module establishes the typed authorization primitives required before
any dashboard update execution can ever be exposed:

* :class:`UpdateApprovalRecord` — an immutable, expiring approval bound to a
  registered project, a canonical plan digest, and an authenticated operator
  session (subject + authentication epoch + session id).
* :class:`UpdateApprovalService` — issuance, validation, and strict single-use
  consumption of those records (``ISSUED → VALID → CONSUMED``, never back).
* :class:`UpdateFlightControl` — process-local per-project single-flight.
* :class:`UpdateApprovalStore` — the narrow persistence protocol implementations
  must satisfy. The canonical MC-6.12 confirmation-store contract
  (:class:`~aipm.control_plane.contracts.ConfirmationStore`) is a superset of
  this protocol, so the canonical durable store can back this service without
  this module importing the control plane.

Single-use semantics: the service performs compare-and-swap style consumption
against the store. Implementations must make ``consume`` atomic from the
perspective of the abstraction (in-process in this slice; durable under the
canonical store contract for production). A second consumption attempt always
fails closed.

No execution capability exists here: no subprocess, no engine invocation, no
routes, no filesystem access beyond what a store implementation itself owns.
"""
from __future__ import annotations

import re
import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

APPROVAL_TTL = timedelta(minutes=10)
MAX_APPROVALS = 256

_APPROVAL_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")


class UpdateApprovalError(Exception):
    """Fail-closed rejection reason for an approval or flight operation."""

    APPROVAL_MALFORMED = "approval_malformed"
    APPROVAL_NOT_FOUND = "approval_not_found"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_ALREADY_CONSUMED = "approval_already_consumed"
    OPERATOR_MISMATCH = "operator_mismatch"
    SESSION_MISMATCH = "session_mismatch"
    PROJECT_MISMATCH = "project_mismatch"
    DIGEST_MISMATCH = "digest_mismatch"
    TIME_PARADOX = "time_paradox"
    STATE_CONFLICT = "state_conflict"

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ApprovalState(str, Enum):
    ISSUED = "issued"
    CONSUMED = "consumed"


class OperatorIdentity:
    """Value object of canonical operator identity material for bindings.

    Mirrors the canonical MC-6.12 identity triple: stable authenticated
    subject, the authentication epoch the principal was issued under, and the
    opaque server-side session identifier. Construct it from an
    ``OwnerSession`` (canonical control-plane session model); nothing else
    may be bound.
    """

    __slots__ = ("subject", "auth_epoch", "session_id")

    def __init__(self, *, subject: str, auth_epoch: int, session_id: str) -> None:
        if not isinstance(subject, str) or not subject or len(subject) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in subject):
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid operator subject")
        if not isinstance(auth_epoch, int) or isinstance(auth_epoch, bool) or auth_epoch < 1:
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid authentication epoch")
        if not isinstance(session_id, str) or _SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid session identifier")
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "auth_epoch", auth_epoch)
        object.__setattr__(self, "session_id", session_id)

    def __setattr__(self, name, value):
        raise AttributeError("OperatorIdentity is immutable")

    def __eq__(self, other):
        if not isinstance(other, OperatorIdentity):
            return NotImplemented
        return (self.subject, self.auth_epoch, self.session_id) == (other.subject, other.auth_epoch, other.session_id)

    def __hash__(self):
        return hash((self.subject, self.auth_epoch, self.session_id))

    def __repr__(self):
        return f"OperatorIdentity(subject={self.subject!r}, auth_epoch={self.auth_epoch}, session_id=<opaque>)"

    @classmethod
    def from_session(cls, session) -> "OperatorIdentity":
        """Build from the canonical OwnerSession (control-plane session model).

        Session freshness is enforced where it is observable: the canonical
        session store revalidates the session at lookup time (epoch, absolute
        and inactivity expiry). This constructor only extracts the canonical
        binding material; approval validation later re-checks epoch and
        session-id equality fail-closed.
        """

        principal = getattr(session, "principal", None)
        epoch = getattr(session, "auth_epoch", None)
        session_id = getattr(session, "session_id", None)
        if principal is None or epoch is None or session_id is None:
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid session model")
        subject = getattr(principal, "subject", None)
        if not isinstance(subject, str) or not subject:
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid session principal")
        return cls(subject=subject, auth_epoch=int(epoch), session_id=session_id)


@dataclass(frozen=True, slots=True)
class UpdateApprovalRecord:
    """Immutable, expiring, operator-bound approval for one canonical plan digest.

    Possession of the approval ID alone grants nothing: every validation and
    consumption re-checks project, digest, operator subject, auth epoch, and
    session id against the presented binding context.
    """

    approval_id: str
    project_id: str
    project_name: str
    plan_digest: str
    operator_subject: str
    auth_epoch: int
    session_id: str
    issued_at: datetime
    expires_at: datetime
    state: ApprovalState = ApprovalState.ISSUED

    def __post_init__(self) -> None:
        if not isinstance(self.approval_id, str) or _APPROVAL_ID_PATTERN.fullmatch(self.approval_id) is None:
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid approval id")
        if not isinstance(self.project_id, str) or not self.project_id or len(self.project_id) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in self.project_id):
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid project id")
        if not isinstance(self.project_name, str) or not self.project_name or len(self.project_name) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in self.project_name):
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid project name")
        if not isinstance(self.plan_digest, str) or _DIGEST_PATTERN.fullmatch(self.plan_digest) is None:
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid plan digest")
        OperatorIdentity(subject=self.operator_subject, auth_epoch=self.auth_epoch, session_id=self.session_id)
        issued = _require_utc(self.issued_at, "issued_at")
        expires = _require_utc(self.expires_at, "expires_at")
        if expires <= issued:
            raise UpdateApprovalError(UpdateApprovalError.TIME_PARADOX, "expires_at must be after issued_at")
        if expires - issued > APPROVAL_TTL:
            raise UpdateApprovalError(UpdateApprovalError.TIME_PARADOX, "Expiry exceeds the approval TTL")
        state = self.state if isinstance(self.state, ApprovalState) else ApprovalState(self.state)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)

    def is_expired(self, now: datetime) -> bool:
        return _require_utc(now, "now") >= self.expires_at

    def safe_dict(self) -> dict[str, str]:
        """API-safe projection: no paths, no tokens, no session internals."""

        return {
            "approval_id": self.approval_id,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "plan_digest": self.plan_digest,
            "state": self.state.value,
            "expires_at": self.expires_at.isoformat(),
        }


def _require_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise UpdateApprovalError(UpdateApprovalError.TIME_PARADOX, f"Invalid {name}")
    if value.tzinfo is None:
        raise UpdateApprovalError(UpdateApprovalError.TIME_PARADOX, f"Naive {name}")
    return value.astimezone(timezone.utc)


@runtime_checkable
class UpdateApprovalStore(Protocol):
    """Narrow persistence face for approval records.

    ``consume`` must be atomic from the abstraction's perspective: it either
    transitions the stored record from ISSUED to CONSUMED exactly once and
    returns the consumed record, or returns ``None`` when the record is
    missing or already consumed. The canonical MC-6.12 ``ConfirmationStore``
    contract satisfies the read/write subset of this protocol with a thin
    adapter, so production can persist approvals durably without this module
    importing the control plane.

    Durability boundary: this protocol standardizes CAS semantics only — it
    says nothing about where records live or how long they survive. Every
    implementation MUST document its durability guarantee explicitly, and
    production update execution MUST inject a store that is durable and
    shared across process boundaries. A process-local store can never
    provide durable replay protection for execution spanning processes,
    restarts, or host failure.
    """

    def put(self, record: UpdateApprovalRecord) -> None: ...

    def get(self, approval_id: str) -> UpdateApprovalRecord | None: ...

    def consume(self, approval_id: str) -> UpdateApprovalRecord | None: ...


class InMemoryUpdateApprovalStore:
    """Bounded in-process store (test double).

    SINGLE-USE SCOPE: PROCESS-LOCAL ONLY. This store guarantees single-use
    consumption only within the lifetime of the process/store instance. It
    MUST NOT be used as the authoritative durable approval store for
    production execution across process boundaries: a restart, a second
    process, or a recreated store instance silently forgets every record,
    which means an approval consumed in one process could be replayed from
    another. Production update execution MUST inject a durable,
    cross-process store implementing :class:`UpdateApprovalStore` (the
    canonical MC-6.12 confirmation store backed by the control plane, via
    adapter). This class is intentionally suitable for tests, local
    dry-run flows, and nothing else.
    """

    def __init__(self, *, max_approvals: int = MAX_APPROVALS) -> None:
        if not isinstance(max_approvals, int) or isinstance(max_approvals, bool) or max_approvals < 1:
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid store bound")
        self._records: dict[str, UpdateApprovalRecord] = {}
        self._max_approvals = max_approvals
        self._lock = threading.Lock()

    def put(self, record: UpdateApprovalRecord) -> None:
        if not isinstance(record, UpdateApprovalRecord):
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid record")
        with self._lock:
            self._records[record.approval_id] = record

    def get(self, approval_id: str) -> UpdateApprovalRecord | None:
        with self._lock:
            return self._records.get(approval_id)

    def consume(self, approval_id: str) -> UpdateApprovalRecord | None:
        with self._lock:
            record = self._records.get(approval_id)
            if record is None or record.state is ApprovalState.CONSUMED:
                return None
            consumed = replace(record, state=ApprovalState.CONSUMED)
            self._records[record.approval_id] = consumed
            return consumed


class UpdateApprovalService:
    """Issues, validates, and consumes operator-bound update approvals.

    ``consume`` implements the single-use CAS: the store transitions the
    record to CONSUMED atomically for the abstraction; a replay attempt finds
    a consumed record and fails closed. Validation never mutates state, and a
    failed binding check never marks the approval consumed.
    """

    def __init__(self, *, store: UpdateApprovalStore, clock: Callable[[], datetime] | None = None, ttl: timedelta = APPROVAL_TTL) -> None:
        if not isinstance(store, UpdateApprovalStore):
            raise TypeError("store must implement UpdateApprovalStore")
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0) or ttl > APPROVAL_TTL:
            raise ValueError("Invalid approval TTL")
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        object.__setattr__(self, "_ttl", ttl)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("UpdateApprovalService is immutable after construction")
        object.__setattr__(self, name, value)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise UpdateApprovalError(UpdateApprovalError.TIME_PARADOX, "Clock returned a non-datetime")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def issue(
        self,
        *,
        project_id: str,
        project_name: str,
        plan_digest: str,
        operator: OperatorIdentity,
        now: datetime | None = None,
    ) -> UpdateApprovalRecord:
        """Issue a fresh, unconsumed approval bound to project+digest+operator."""

        now_value = self._now() if now is None else _require_utc(now, "now")
        record = UpdateApprovalRecord(
            approval_id=secrets.token_hex(16),
            project_id=project_id,
            project_name=project_name,
            plan_digest=plan_digest,
            operator_subject=operator.subject,
            auth_epoch=operator.auth_epoch,
            session_id=operator.session_id,
            issued_at=now_value,
            expires_at=now_value + self._ttl,
            state=ApprovalState.ISSUED,
        )
        self._store.put(record)
        return record

    def validate(
        self,
        approval_id: str,
        *,
        project_id: str,
        plan_digest: str,
        operator: OperatorIdentity,
        now: datetime | None = None,
    ) -> UpdateApprovalRecord:
        """Return the stored record if every binding dimension matches; else fail closed."""

        if not isinstance(approval_id, str) or _APPROVAL_ID_PATTERN.fullmatch(approval_id) is None:
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid approval id")
        record = self._store.get(approval_id)
        if record is None:
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_NOT_FOUND, "Unknown approval")
        if not isinstance(operator, OperatorIdentity):
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid operator identity")
        if record.state is ApprovalState.CONSUMED:
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_ALREADY_CONSUMED, "Approval was already consumed")
        if record.is_expired(self._now() if now is None else _require_utc(now, "now")):
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_EXPIRED, "Approval has expired")
        if record.project_id != project_id:
            raise UpdateApprovalError(UpdateApprovalError.PROJECT_MISMATCH, "Project does not match")
        if record.plan_digest != plan_digest:
            raise UpdateApprovalError(UpdateApprovalError.DIGEST_MISMATCH, "Plan digest does not match")
        if record.operator_subject != operator.subject:
            raise UpdateApprovalError(UpdateApprovalError.OPERATOR_MISMATCH, "Operator does not match")
        if record.auth_epoch != operator.auth_epoch:
            raise UpdateApprovalError(UpdateApprovalError.SESSION_MISMATCH, "Authentication epoch does not match")
        if record.session_id != operator.session_id:
            raise UpdateApprovalError(UpdateApprovalError.SESSION_MISMATCH, "Session does not match")
        return record

    def consume(
        self,
        approval_id: str,
        *,
        project_id: str,
        plan_digest: str,
        operator: OperatorIdentity,
        now: datetime | None = None,
    ) -> UpdateApprovalRecord:
        """Validate every binding dimension, then transition ISSUED → CONSUMED once.

        All binding checks run against the stored record BEFORE the atomic
        store transition; a failed check never marks the approval consumed. A
        racing consumer or replay that won the transition causes a fail-closed
        error here.
        """

        record = self.validate(approval_id, project_id=project_id, plan_digest=plan_digest, operator=operator, now=now)
        consumed = self._store.consume(record.approval_id)
        if consumed is None:
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_ALREADY_CONSUMED, "Approval was already consumed")
        return consumed


class UpdateFlightControl:
    """Per-project single-flight primitive for future update execution.

    Scope: PROCESS-LOCAL mutual exclusion keyed by registered project name.
    It does NOT provide distributed or cross-process guarantees. Contention
    policy: a second acquire for a held project fails immediately (fail-fast,
    non-blocking), so two updates for the same project can never overlap in
    one process; unrelated projects never block each other.
    """

    def __init__(self) -> None:
        self._held: dict[str, _FlightToken] = {}
        self._lock = threading.Lock()

    @contextmanager
    def acquire(self, project_name: str) -> Iterator["_FlightToken"]:
        token = self._try_acquire(project_name)
        try:
            yield token
        finally:
            self.release(token)

    def _try_acquire(self, project_name: str) -> "_FlightToken":
        if (
            not isinstance(project_name, str)
            or not project_name
            or len(project_name) > 128
            or any(ord(c) < 32 or ord(c) == 127 for c in project_name)
            or "/" in project_name
            or "\\" in project_name
        ):
            raise UpdateApprovalError(UpdateApprovalError.APPROVAL_MALFORMED, "Invalid project identifier")
        with self._lock:
            if project_name in self._held:
                raise UpdateApprovalError(UpdateApprovalError.STATE_CONFLICT, "An update flight is already active for this project")
            token = _FlightToken(project_name)
            self._held[project_name] = token
            return token

    def release(self, token: "_FlightToken") -> bool:
        with self._lock:
            if not isinstance(token, _FlightToken) or self._held.get(token.project_name) is not token:
                return False
            del self._held[token.project_name]
            return True


@dataclass(frozen=True, slots=True)
class _FlightToken:
    project_name: str
