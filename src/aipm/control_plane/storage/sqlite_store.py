"""Durable SQLite implementation of the control-plane storage contracts.

This is the production authority for ProjectPlan, action/decision,
confirmation, kill-switch, lease, and snapshot state. It is completely
separate from the telemetry/events/incidents/notification databases: own file,
own path, own schema versioning, DELETE journal mode, owner-only permissions.

The database stores exactly the canonical values issued by the domain layer:
no identity re-derivation, no second digest algorithm, no alternate state
machine. Rows are validated against the domain contracts on every load and
any corruption fails closed with a bounded error. Nothing here can execute,
spawn, or mutate anything outside this database file.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from aipm.control_plane.action_state import validate_action_registration
from aipm.control_plane.contracts import LifecycleTransition
from aipm.control_plane.identity import (
    ActionIdentity,
    verify_action_identity,
)
from aipm.control_plane.kill_switch import KillSwitch, KillSwitchState
from aipm.control_plane.lifecycle import advance as advance_lifecycle
from aipm.control_plane.lifecycle import validate_transition
from aipm.control_plane.models import (
    ActionLifecycle,
    ActionRequest,
    ConfirmationBinding,
    ConfirmationKind,
    ConfirmationState,
    ControlPlaneError,
    LifecycleState,
    OperationKind,
    PlanningErrorCode,
)
from aipm.control_plane.policy import AuthorizationDecision
from aipm.control_plane.project_plan import Environment, PlanConflict, ProjectPlan, ProjectPlanError

DATABASE_FILENAME = "control_plane.db"
SNAPSHOT_VERSION_DEFAULT = "mc612-snapshot-v1"
SESSION_VERSION_DEFAULT = "mc612-session-v1"
DEFAULT_LEASE_TTL = __import__("datetime").timedelta(minutes=5)
DEFAULT_STATE_ROOT = Path.home() / ".local" / "state" / "aipm" / "control_plane"
_BUSY_TIMEOUT_MS = 5000
_ACTIVE_CONFIRMATION_STATES = ("confirmation_requested", "confirmed")
_HEX64 = "^[0-9a-f]{64}$"


class ControlPlaneStorageUnavailable(ValueError):
    """Raised when the control-plane database cannot be initialized safely."""


def default_database_path() -> Path:
    """Resolve the dedicated control-plane database path.

    ``AIPM_CONTROL_PLANE_DB`` overrides the default state root. The telemetry
    database path is never consulted.
    """

    override = os.environ.get("AIPM_CONTROL_PLANE_DB", "").strip()
    if override:
        return Path(override)
    return DEFAULT_STATE_ROOT / DATABASE_FILENAME


def _corrupt(message: str) -> ControlPlaneError:
    return ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, message)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _parse_timestamp(value: str, *, name: str) -> datetime:
    if not isinstance(value, str):
        raise _corrupt(f"Invalid stored {name}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _corrupt(f"Invalid stored {name}") from exc
    if parsed.tzinfo is None:
        raise _corrupt(f"Invalid stored {name}")
    return parsed


class ControlPlaneDatabase:
    """Owns the dedicated control-plane SQLite database."""

    __slots__ = ("_path", "_connection", "_clock", "_write_lock", "_initialized")

    def __init__(self, path: str | Path | None = None, *, clock=None) -> None:
        resolved = Path(path) if path is not None else default_database_path()
        self._prepare_filesystem(resolved)
        try:
            connection = sqlite3.connect(str(resolved), timeout=_BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
        except sqlite3.Error as exc:
            raise ControlPlaneStorageUnavailable("control-plane database cannot be opened") from exc
        object.__setattr__(self, "_path", resolved)
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        object.__setattr__(self, "_write_lock", threading.Lock())
        object.__setattr__(self, "_initialized", True)
        self._configure_connection()
        self._ensure_schema()
        self._seed_kill_switch_defaults()

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("ControlPlaneDatabase configuration is immutable")
        object.__setattr__(self, name, value)

    # -- lifecycle ----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def journal_mode(self) -> str:
        row = self._connection.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]) if row else ""

    def schema_version(self) -> int:
        from aipm.control_plane.storage.schema import SCHEMA_NAME

        row = self._connection.execute(
            "SELECT schema_version FROM control_plane_schema_meta WHERE schema_name = ?",
            (SCHEMA_NAME,),
        ).fetchone()
        return int(row[0]) if row else 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Exclusive write-transaction scope.

        Acquires the connection write lock, opens an explicit
        ``BEGIN IMMEDIATE`` transaction (so read-then-write composites hold
        the database write lock from the start), commits on success, and rolls
        back on any error. The database remains the serialization authority:
        separate processes rely purely on SQLite locking and constraints.
        """

        with self._write_lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _prepare_filesystem(resolved: Path) -> None:
        parent = resolved.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            os.chmod(parent, 0o700)
        except OSError as exc:
            raise ControlPlaneStorageUnavailable("control-plane state directory cannot be prepared safely") from exc
        if resolved.exists():
            mode = stat.S_IMODE(os.stat(resolved).st_mode)
            if mode & 0o077:
                raise ControlPlaneStorageUnavailable("control-plane database permissions are too open")
            return
        try:
            connection = sqlite3.connect(str(resolved))
            connection.close()
            os.chmod(resolved, 0o600)
        except OSError as exc:
            raise ControlPlaneStorageUnavailable("control-plane database cannot be created safely") from exc

    def _configure_connection(self) -> None:
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode = DELETE")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        except sqlite3.Error as exc:
            raise ControlPlaneStorageUnavailable("control-plane database cannot be configured") from exc
        if self.journal_mode() != "delete":
            raise ControlPlaneStorageUnavailable("control-plane database must use the rollback journal")

    def _ensure_schema(self) -> None:
        from aipm.control_plane.storage.schema import SCHEMA_NAME, SCHEMA_VERSION, schema_statements_for_version

        try:
            with self._connection:
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS control_plane_schema_meta ("
                    " schema_name TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, migrated_at TEXT NOT NULL)"
                )
                row = self._connection.execute(
                    "SELECT schema_version FROM control_plane_schema_meta WHERE schema_name = ?",
                    (SCHEMA_NAME,),
                ).fetchone()
                if row is None:
                    statements = schema_statements_for_version(0)
                else:
                    stored_version = int(row[0])
                    if stored_version > SCHEMA_VERSION:
                        raise ControlPlaneStorageUnavailable("control-plane database schema is from a newer version")
                    statements = schema_statements_for_version(stored_version)
                for statement in statements:
                    self._connection.execute(statement)
                self._apply_column_migrations()
                self._connection.execute(
                    "INSERT INTO control_plane_schema_meta (schema_name, schema_version, migrated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(schema_name) DO UPDATE SET schema_version = excluded.schema_version,"
                    " migrated_at = excluded.migrated_at",
                    (SCHEMA_NAME, SCHEMA_VERSION, self._now_iso()),
                )
        except ControlPlaneStorageUnavailable:
            raise
        except sqlite3.Error as exc:
            raise ControlPlaneStorageUnavailable("control-plane schema cannot be initialized") from exc

    def _apply_column_migrations(self) -> None:
        """Deterministically add columns that pre-v3 databases are missing."""

        from aipm.control_plane.storage.schema import COLUMN_MIGRATIONS

        for table, columns in COLUMN_MIGRATIONS.items():
            existing = {row[1] for row in self._connection.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue
            for name, declaration in columns:
                if name not in existing:
                    self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def _seed_kill_switch_defaults(self) -> None:
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT OR IGNORE INTO kill_switch_state (environment, state, epoch, reason, actor_subject, created_at, updated_at)"
                    " VALUES ('staging', 'engaged', 1, '', NULL, ?, ?)",
                    (self._now_iso(), self._now_iso()),
                )
                self._connection.execute(
                    "INSERT OR IGNORE INTO kill_switch_state (environment, state, epoch, reason, actor_subject, created_at, updated_at)"
                    " VALUES ('production', 'permanent', 1, '', NULL, ?, ?)",
                    (self._now_iso(), self._now_iso()),
                )
        except sqlite3.Error as exc:
            raise ControlPlaneStorageUnavailable("kill-switch defaults cannot be seeded") from exc

    def _now_iso(self) -> str:
        return _utc(self._clock()).isoformat()


# ---------------------------------------------------------------------------
# Row mappers (fail closed on any corruption)
# ---------------------------------------------------------------------------


def _request_from_canonical(canonical: str) -> ActionRequest:
    try:
        payload = json.loads(canonical)
        request = ActionRequest(
            operation=OperationKind(payload["operation"]),
            target_id=payload["target_id"],
            idempotency_key=payload["idempotency_key"],
            metadata=tuple((item[0], item[1]) for item in payload["metadata"]),
            environment=payload["environment"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _corrupt("Stored request cannot be reconstructed") from exc
    if request.canonical() != canonical:
        raise _corrupt("Stored request does not match its canonical form")
    return request


def _identity_from_row(row: sqlite3.Row, *, requester_column: str = "requester_subject") -> ActionIdentity:
    try:
        identity = ActionIdentity(
            action_id=row["action_id"],
            plan_id=row["plan_id"],
            plan_digest=row["plan_digest"],
            target_revision=int(row["plan_revision"]),
            target_digest=row["target_digest"],
            policy_version=row["policy_version"],
            requester_subject=row[requester_column],
            operation=row["operation"],
            target_id=row["target_id"],
            environment=row["environment"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _corrupt("Stored action identity cannot be reconstructed") from exc
    if not verify_action_identity(identity):
        raise _corrupt("Stored action identity failed verification")
    return identity


def _decision_from_row(row: sqlite3.Row) -> AuthorizationDecision:
    from aipm.control_plane.policy import PolicyCode

    try:
        decision = AuthorizationDecision(
            decision_id=row["decision_id"],
            allowed=bool(row["allowed"]),
            code=PolicyCode(row["code"]),
            operation=OperationKind(row["operation"]),
            target_id=row["target_id"],
            environment=row["environment"],
            policy_version=row["policy_version"],
            principal_subject=row["principal_subject"],
            confirmation_required=bool(row["confirmation_required"]),
            decided_at=_parse_timestamp(row["decided_at"], name="decision timestamp"),
            expires_at=_parse_timestamp(row["expires_at"], name="decision expiry"),
            action_identity=_identity_from_row(row, requester_column="principal_subject"),
            plan_revision=int(row["plan_revision"]),
            plan_digest=row["plan_digest"],
            confirmation_kind=ConfirmationKind(row["confirmation_kind"]),
            request=_request_from_canonical(row["request_canonical"]),
        )
    except ControlPlaneError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _corrupt("Stored decision cannot be reconstructed") from exc
    return decision


def _lifecycle_from_row(row: sqlite3.Row) -> ActionLifecycle:
    try:
        state = LifecycleState(row["lifecycle_state"])
        if not isinstance(row["version"], int) or row["version"] < 0:
            raise _corrupt("Invalid stored action version")
        _identity_from_row(row)
        lifecycle = ActionLifecycle(
            action_id=row["action_id"],
            plan_id=row["plan_id"],
            decision_id=row["decision_id"],
            plan_revision=int(row["plan_revision"]),
            rollback_of_action_id=row["rollback_of_action_id"],
            snapshot_id=row["snapshot_id"],
            plan_digest=row["plan_digest"],
            operation=OperationKind(row["operation"]),
            scope=_scope_from_row(row),
            state=state,
            requester_subject=row["requester_subject"],
            confirmation_kind=ConfirmationKind(row["confirmation_kind"]),
            approver_subject=row["approver_subject"],
            idempotency_key=row["idempotency_key"],
            created_at=_parse_timestamp(row["created_at"], name="action timestamp"),
            expires_at=_parse_timestamp(row["expires_at"], name="action expiry"),
            version=int(row["version"]),
        )
    except ControlPlaneError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _corrupt("Stored action cannot be reconstructed") from exc
    return lifecycle


def _scope_from_row(row: sqlite3.Row):
    from aipm.control_plane.models import ActionScope

    return ActionScope(target_id=row["target_id"], environment=row["environment"], policy_version=row["policy_version"])


def _confirmation_from_row(row: sqlite3.Row) -> ConfirmationBinding:
    try:
        binding = ConfirmationBinding(
            confirmation_id=row["confirmation_id"],
            decision_id=row["decision_id"],
            action_id=row["action_id"],
            plan_id=row["plan_id"],
            plan_digest=row["plan_digest"],
            target_revision=int(row["target_revision"]),
            target_digest=row["target_digest"],
            policy_version=row["policy_version"],
            requester_subject=row["requester_subject"],
            confirmation_kind=ConfirmationKind(row["confirmation_kind"]),
            request=_request_from_canonical(row["request_canonical"]),
            created_at=_parse_timestamp(row["created_at"], name="confirmation timestamp"),
            expires_at=_parse_timestamp(row["expires_at"], name="confirmation expiry"),
            confirmed_by_subject=row["confirmed_by_subject"],
            scope=row["scope"],
            state=ConfirmationState(row["state"]),
        )
    except ControlPlaneError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _corrupt("Stored confirmation cannot be reconstructed") from exc
    return binding


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


def _plan_row_to_domain(row: sqlite3.Row) -> ProjectPlan:
    try:
        environment = Environment(row["environment"])
        revision = int(row["revision"])
        enabled = bool(row["enabled"])
        created_at = _parse_timestamp(row["created_at"], name="plan timestamp")
        updated_at = _parse_timestamp(row["updated_at"], name="plan update timestamp")
        plan = ProjectPlan(
            target_id=row["target_id"],
            environment=environment,
            revision=revision,
            title=row["title"],
            objective=row["objective"],
            created_at=created_at,
            updated_at=updated_at,
            enabled=enabled,
            canonical_digest=row["canonical_digest"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _corrupt("Stored ProjectPlan cannot be reconstructed") from exc
    if revision < 1 or plan.digest() != plan.canonical_digest:
        raise _corrupt("Stored ProjectPlan failed integrity verification")
    return plan


class SQLiteProjectPlanStore:
    """Durable ProjectPlan store with expected-revision CAS updates."""

    __slots__ = ("_db", "_initialized")

    def __init__(self, db: ControlPlaneDatabase) -> None:
        if not isinstance(db, ControlPlaneDatabase):
            raise TypeError("SQLiteProjectPlanStore requires a ControlPlaneDatabase")
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("SQLiteProjectPlanStore configuration is immutable")
        object.__setattr__(self, name, value)

    def create(self, plan: ProjectPlan) -> ProjectPlan:
        if not isinstance(plan, ProjectPlan) or plan.environment is not Environment.STAGING:
            raise ProjectPlanError("only staging ProjectPlans are enabled")
        try:
            with self._db.transaction():
                self._db.connection.execute(
                    "INSERT INTO project_plans (target_id, environment, revision, title, objective, enabled, canonical_digest, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        plan.target_id,
                        plan.environment.value,
                        plan.revision,
                        plan.title,
                        plan.objective,
                        1 if plan.enabled else 0,
                        plan.canonical_digest,
                        plan.created_at.isoformat(),
                        plan.updated_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PlanConflict("target already exists") from exc
        except sqlite3.Error as exc:
            raise ProjectPlanError("ProjectPlan cannot be stored") from exc
        return plan

    def read(self, target_id: str) -> ProjectPlan:
        row = self._db.connection.execute(
            "SELECT target_id, environment, revision, title, objective, enabled, canonical_digest, created_at, updated_at"
            " FROM project_plans WHERE target_id = ?",
            (target_id,),
        ).fetchone()
        if row is None:
            raise ProjectPlanError("target is not registered")
        return self._plan_from_row(row)

    def update(self, target_id: str, *, expected_revision: int, fields, now) -> ProjectPlan:
        current = self.read(target_id)
        updated = current.update(expected_revision=expected_revision, fields=fields, now=now)
        try:
            with self._db.transaction():
                cursor = self._db.connection.execute(
                    "UPDATE project_plans SET revision = ?, title = ?, objective = ?, canonical_digest = ?, updated_at = ?"
                    " WHERE target_id = ? AND revision = ?",
                    (
                        updated.revision,
                        updated.title,
                        updated.objective,
                        updated.canonical_digest,
                        updated.updated_at.isoformat(),
                        target_id,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PlanConflict("stale project plan revision")
        except sqlite3.Error as exc:
            raise ProjectPlanError("ProjectPlan cannot be updated") from exc
        return updated

    def _plan_from_row(self, row: sqlite3.Row) -> ProjectPlan:
        return _plan_row_to_domain(row)




class SQLiteActionRepository:
    """Durable authority for decisions, actions, and confirmations.

    When an audit sink (the SQLiteAuditLedger on the same database) is
    attached, caller-supplied audit drafts are appended inside the SAME
    transaction as the state write: a state transition without durable
    evidence is impossible, and an evidence failure rolls the state back.
    """

    __slots__ = ("_db", "_audit", "_initialized")

    def __init__(self, db: ControlPlaneDatabase, *, audit=None) -> None:
        if not isinstance(db, ControlPlaneDatabase):
            raise TypeError("SQLiteActionRepository requires a ControlPlaneDatabase")
        if audit is not None and not hasattr(audit, "append_in_transaction"):
            raise TypeError("audit must provide append_in_transaction")
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "_audit", audit)
        object.__setattr__(self, "_initialized", True)

    def _append_evidence(self, drafts) -> None:
        if not self._audit or not drafts:
            return
        for draft in drafts:
            self._audit.append_in_transaction(draft)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("SQLiteActionRepository configuration is immutable")
        object.__setattr__(self, name, value)

    # -- decisions + actions ------------------------------------------------

    def register_action(self, decision, lifecycle, *, audit_drafts=()) -> None:
        validate_action_registration(decision, lifecycle)
        identity = decision.action_identity
        assert identity is not None
        request = decision.request
        if request is None:
            raise _corrupt("Decision carries no request")
        try:
            with self._db.transaction():
                self._db.connection.execute(
                    "INSERT INTO authorization_decisions (decision_id, action_id, allowed, code, operation, target_id, environment,"
                    " policy_version, principal_subject, confirmation_required, confirmation_kind, plan_id, plan_revision, plan_digest,"
                    " target_digest, request_canonical, decided_at, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        decision.decision_id,
                        identity.action_id,
                        1 if decision.allowed else 0,
                        decision.code.value,
                        identity.operation,
                        identity.target_id,
                        identity.environment,
                        identity.policy_version,
                        decision.principal_subject,
                        1 if decision.confirmation_required else 0,
                        decision.confirmation_kind.value,
                        identity.plan_id,
                        identity.target_revision,
                        identity.plan_digest,
                        identity.target_digest,
                        request.canonical(),
                        decision.decided_at.isoformat(),
                        decision.expires_at.isoformat(),
                    ),
                )
                self._insert_action_row(decision, lifecycle)
                self._append_evidence(audit_drafts)
        except sqlite3.IntegrityError as exc:
            self._handle_registration_conflict(decision, lifecycle, exc)
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Action cannot be registered") from exc

    def _insert_action_row(self, decision, lifecycle) -> None:
        identity = decision.action_identity
        assert identity is not None
        self._db.connection.execute(
            "INSERT INTO actions (action_id, decision_id, idempotency_key, operation, target_id, environment, plan_id,"
            " plan_revision, plan_digest, target_digest, requester_subject, policy_version, lifecycle_state,"
            " confirmation_kind, approver_subject, created_at, updated_at, expires_at, version,"
            " rollback_of_action_id, snapshot_id, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lifecycle.action_id,
                decision.decision_id,
                lifecycle.idempotency_key,
                identity.operation,
                identity.target_id,
                identity.environment,
                identity.plan_id,
                identity.target_revision,
                identity.plan_digest,
                identity.target_digest,
                identity.requester_subject,
                identity.policy_version,
                lifecycle.state.value,
                lifecycle.confirmation_kind.value,
                lifecycle.approver_subject,
                lifecycle.created_at.isoformat(),
                lifecycle.created_at.isoformat(),
                lifecycle.expires_at.isoformat(),
                lifecycle.version,
                lifecycle.rollback_of_action_id,
                lifecycle.snapshot_id,
                "mutation_not_started",
            ),
        )

    def _handle_registration_conflict(self, decision, lifecycle, exc: sqlite3.Error) -> None:
        existing = self.find_action_by_idempotency(
            target_id=lifecycle.scope.target_id,
            operation=lifecycle.operation.value,
            idempotency_key=lifecycle.idempotency_key,
        )
        if existing is not None:
            if existing.action_id == lifecycle.action_id:
                return
            raise ControlPlaneError(PlanningErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency key already bound to a different request") from exc
        raise _corrupt("Duplicate action identity") from exc

    def get_decision(self, decision_id: str):
        row = self._db.connection.execute(
            "SELECT * FROM authorization_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        return _decision_from_row(row)

    def get_action(self, action_id: str):
        row = self._db.connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            return None
        return _lifecycle_from_row(row)

    def find_action_by_idempotency(self, *, target_id: str, operation: str, idempotency_key: str):
        row = self._db.connection.execute(
            "SELECT * FROM actions WHERE target_id = ? AND operation = ? AND idempotency_key = ?",
            (target_id, operation, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return _lifecycle_from_row(row)

    def advance_action(self, action_id: str, *, expected_version: int, next_state, approver_subject: str, now, audit_drafts=()) -> ActionLifecycle:
        row = self._db.connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise _corrupt("Unknown action")
        current = _lifecycle_from_row(row)
        from aipm.control_plane.models import LifecycleState as _LifecycleState

        if next_state == _LifecycleState.SNAPSHOT_CAPTURED or (isinstance(next_state, str) and next_state == "snapshot_captured"):
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Snapshot capture requires the composite snapshot transition")
        validate_transition(current, next_state, now=now, actor_subject=approver_subject)
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        advanced = advance_lifecycle(current, next_state, now=now, actor_subject=approver_subject)
        try:
            with self._db.transaction():
                cursor = self._db.connection.execute(
                    "UPDATE actions SET lifecycle_state = ?, approver_subject = ?, version = ?, updated_at = ?"
                    " WHERE action_id = ? AND version = ?",
                    (
                        advanced.state.value,
                        advanced.approver_subject,
                        advanced.version,
                        _utc(now).isoformat() if now is not None else self._now_iso(),
                        action_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
                self._append_evidence(audit_drafts)
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Action transition cannot be persisted") from exc
        return advanced

    def capture_snapshot_and_advance(
        self,
        snapshot,
        *,
        action_id: str,
        expected_version: int,
        now,
        audit_drafts=(),
    ) -> ActionLifecycle:
        """Atomically insert the pre-mutation snapshot and advance the action.

        The snapshot must be bound to the action's exact bound plan revision;
        a stale snapshot can never authorize the transition. The lifecycle
        cannot represent SNAPSHOT_CAPTURED without a durably committed
        snapshot because this composite is the only transition path.
        """

        from aipm.control_plane.models import LifecycleState as _LifecycleState

        if not isinstance(snapshot, PlanSnapshot):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid plan snapshot")
        row = self._db.connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise _corrupt("Unknown action")
        current = _lifecycle_from_row(row)
        if current.state is not _LifecycleState.CONFIRMED:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Snapshot capture requires a confirmed action")
        if snapshot.action_id != action_id:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Snapshot is bound to a different action")
        if snapshot.revision != current.plan_revision:
            raise ControlPlaneError(PlanningErrorCode.STALE_EVIDENCE, "Snapshot is stale against the action revision")
        validate_transition(current, _LifecycleState.SNAPSHOT_CAPTURED, now=now)
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        advanced = advance_lifecycle(current, _LifecycleState.SNAPSHOT_CAPTURED, now=now)
        try:
            with self._db.transaction():
                _insert_snapshot_row(self._db.connection, snapshot)
                cursor = self._db.connection.execute(
                    "UPDATE actions SET lifecycle_state = ?, version = ?, updated_at = ?"
                    " WHERE action_id = ? AND version = ? AND lifecycle_state = ?",
                    (
                        advanced.state.value,
                        advanced.version,
                        _utc(now).isoformat() if now is not None else self._now_iso(),
                        action_id,
                        expected_version,
                        current.state.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
                self._append_evidence(audit_drafts)
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Snapshot capture cannot be persisted") from exc
        return advanced

    def mark_outcome(self, action_id: str, *, expected_version: int, outcome: str, now, audit_drafts=()) -> ActionLifecycle:
        """Record the execution outcome classification, CAS-guarded.

        ``UNKNOWN_OUTCOME`` may only be replaced by a reconciled definitive
        outcome; it can never be reset to ``mutation_not_started`` — that
        would open the blind-retry loophole the classification exists to
        close.
        """

        from aipm.control_plane.verification import ExecutionOutcome

        try:
            normalized = outcome if isinstance(outcome, ExecutionOutcome) else ExecutionOutcome(outcome)
        except ValueError as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid execution outcome") from exc
        row = self._db.connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise _corrupt("Unknown action")
        current = _lifecycle_from_row(row)
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        stored_outcome = row["outcome"]
        if stored_outcome == ExecutionOutcome.UNKNOWN_OUTCOME.value and normalized is ExecutionOutcome.MUTATION_NOT_STARTED:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Unknown outcome forbids reset to not-started")
        try:
            with self._db.transaction():
                cursor = self._db.connection.execute(
                    "UPDATE actions SET outcome = ?, updated_at = ? WHERE action_id = ? AND version = ?",
                    (
                        normalized.value,
                        _utc(now).isoformat() if now is not None else self._now_iso(),
                        action_id,
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
                self._append_evidence(audit_drafts)
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Execution outcome cannot be persisted") from exc
        return current

    def active_lease(self, action_id: str, *, now=None):
        moment = _utc(now).isoformat() if now is not None else self._now_iso()
        row = self._db.connection.execute(
            "SELECT lease_id FROM execution_leases WHERE action_id = ? AND state = 'granted' AND expires_at > ?"
            " ORDER BY fencing_token DESC LIMIT 1",
            (action_id, moment),
        ).fetchone()
        if row is None:
            return None
        from aipm.control_plane.storage.sqlite_store import SQLiteLeaseRepository

        return SQLiteLeaseRepository(self._db).get(row["lease_id"])

    def acquire_lease(self, action_id: str, expected_version: int, *, now, audit_drafts=()):
        """Grant the one active lease for a snapshot-captured action.

        The lease is unique per action (one granted lease at a time), bounded
        in time, fencing-token monotonic per action, and bound to the action
        version. The grant, the CAS transition to LEASED, and the audit
        evidence share one transaction.
        """

        import secrets as _secrets
        from datetime import timedelta as _timedelta

        from aipm.control_plane.audit import builders as audit_builders
        from aipm.control_plane.models import LifecycleState as _LifecycleState
        from aipm.control_plane.storage.sqlite_store import DEFAULT_LEASE_TTL, ExecutionLease

        row = self._db.connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise _corrupt("Unknown action")
        current = _lifecycle_from_row(row)
        if current.state is not _LifecycleState.SNAPSHOT_CAPTURED:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Lease acquisition requires a snapshot-captured action")
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        if current.is_expired(now or self._now_iso()):
            raise ControlPlaneError(PlanningErrorCode.EXPIRED_PLAN, "Action has expired")
        validate_transition(current, _LifecycleState.LEASED, now=now)
        granted_at = _utc(now) if now is not None else datetime.now(timezone.utc)
        expires_at = granted_at + DEFAULT_LEASE_TTL
        advanced = advance_lifecycle(current, _LifecycleState.LEASED, now=granted_at)
        try:
            with self._db.transaction():
                existing = self._db.connection.execute(
                    "SELECT lease_id FROM execution_leases WHERE action_id = ? AND state = 'granted' AND expires_at > ?",
                    (action_id, granted_at.isoformat()),
                ).fetchone()
                if existing is not None:
                    raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "An active lease already exists for this action")
                token_row = self._db.connection.execute(
                    "SELECT COALESCE(MAX(fencing_token), 0) AS top FROM execution_leases WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                fencing_token = int(token_row["top"]) + 1
                lease = ExecutionLease(
                    lease_id=_secrets.token_hex(16),
                    action_id=action_id,
                    environment=current.scope.environment,
                    fencing_token=fencing_token,
                    state="granted",
                    granted_at=granted_at,
                    expires_at=expires_at,
                    action_version=advanced.version,
                )
                self._db.connection.execute(
                    "INSERT INTO execution_leases (lease_id, action_id, environment, fencing_token, state, holder, granted_at, expires_at, released_at, action_version)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        lease.lease_id,
                        lease.action_id,
                        lease.environment,
                        lease.fencing_token,
                        lease.state,
                        lease.holder,
                        lease.granted_at.isoformat(),
                        lease.expires_at.isoformat(),
                        None,
                        lease.action_version,
                    ),
                )
                cursor = self._db.connection.execute(
                    "UPDATE actions SET lifecycle_state = ?, version = ?, updated_at = ?"
                    " WHERE action_id = ? AND version = ? AND lifecycle_state = ?",
                    (
                        advanced.state.value,
                        advanced.version,
                        granted_at.isoformat(),
                        action_id,
                        expected_version,
                        current.state.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
                self._append_evidence(audit_drafts or (
                    audit_builders.lease_acquired(
                        actor_subject="control-plane-system",
                        occurred_at=granted_at,
                        action_id=action_id,
                        plan_id=current.plan_id,
                        target_id=current.scope.target_id,
                        environment=current.scope.environment,
                        lease_id=lease.lease_id,
                        fencing_token=fencing_token,
                    ),
                ))
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Lease cannot be granted") from exc
        return lease, advanced

    def begin_execution(self, action_id: str, expected_version: int, *, confirmation_id: str, now, audit_drafts=()) -> ActionLifecycle:
        """Consume the confirmation exactly once and enter RUNNING, atomically."""

        from aipm.control_plane.models import LifecycleState as _LifecycleState

        row = self._db.connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise _corrupt("Unknown action")
        current = _lifecycle_from_row(row)
        if current.state is not _LifecycleState.LEASED:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Execution start requires a leased action")
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        validate_transition(current, _LifecycleState.RUNNING, now=now)
        advanced = advance_lifecycle(current, _LifecycleState.RUNNING, now=now)
        try:
            with self._db.transaction():
                consumed = self._db.connection.execute(
                    "UPDATE confirmations SET state = 'consumed' WHERE confirmation_id = ? AND state = 'confirmed'",
                    (confirmation_id,),
                )
                if consumed.rowcount != 1:
                    raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Confirmation is not consumable")
                cursor = self._db.connection.execute(
                    "UPDATE actions SET lifecycle_state = ?, version = ?, updated_at = ?"
                    " WHERE action_id = ? AND version = ? AND lifecycle_state = ?",
                    (
                        advanced.state.value,
                        advanced.version,
                        _utc(now).isoformat() if now is not None else self._now_iso(),
                        action_id,
                        expected_version,
                        current.state.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
                self._append_evidence(audit_drafts)
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Execution cannot begin") from exc
        return advanced

    def execute_plan_mutation(self, action_id: str, expected_version: int, *, expected_revision: int, mutation_fields, now, audit_drafts=()):
        """Atomically apply the CAS plan mutation and advance the lifecycle.

        The plan row update (revision CAS), the action transition to
        EXECUTED_PENDING_VERIFICATION with outcome ``mutation_succeeded``, and
        the audit evidence commit or roll back together: for a control-plane
        durable mutation there is no window in which the effect happened
        without the durable success state.
        """

        from aipm.control_plane.models import LifecycleState as _LifecycleState

        action_row = self._db.connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if action_row is None:
            raise _corrupt("Unknown action")
        current = _lifecycle_from_row(action_row)
        if current.state is not _LifecycleState.RUNNING:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Plan mutation requires a running action")
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        plan_row = self._db.connection.execute(
            "SELECT * FROM project_plans WHERE target_id = ?",
            (current.scope.target_id,),
        ).fetchone()
        if plan_row is None:
            raise _corrupt("Target plan is missing")
        plan = _plan_row_to_domain(plan_row)
        updated = plan.update(expected_revision=expected_revision, fields=mutation_fields, now=now or self._now_iso())
        advanced = advance_lifecycle(current, _LifecycleState.EXECUTED_PENDING_VERIFICATION, now=now)
        try:
            with self._db.transaction():
                plan_cursor = self._db.connection.execute(
                    "UPDATE project_plans SET revision = ?, title = ?, objective = ?, canonical_digest = ?, updated_at = ?"
                    " WHERE target_id = ? AND revision = ?",
                    (
                        updated.revision,
                        updated.title,
                        updated.objective,
                        updated.canonical_digest,
                        updated.updated_at.isoformat(),
                        current.scope.target_id,
                        expected_revision,
                    ),
                )
                if plan_cursor.rowcount != 1:
                    raise ControlPlaneError(PlanningErrorCode.STALE_EVIDENCE, "Current plan no longer matches the authorized precondition")
                action_cursor = self._db.connection.execute(
                    "UPDATE actions SET lifecycle_state = ?, outcome = ?, version = ?, updated_at = ?"
                    " WHERE action_id = ? AND version = ? AND lifecycle_state = ?",
                    (
                        advanced.state.value,
                        "mutation_succeeded",
                        advanced.version,
                        _utc(now).isoformat() if now is not None else self._now_iso(),
                        action_id,
                        expected_version,
                        current.state.value,
                    ),
                )
                if action_cursor.rowcount != 1:
                    raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
                self._append_evidence(audit_drafts)
        except ControlPlaneError:
            raise
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STALE_EVIDENCE, "Plan mutation cannot be persisted") from exc
        return advanced, None

    def record_verification_outcome(self, action_id: str, expected_version: int, *, result, now, audit_drafts=()) -> ActionLifecycle:
        """Persist the verification record and the lifecycle verdict atomically."""

        from aipm.control_plane.models import LifecycleState as _LifecycleState

        row = self._db.connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise _corrupt("Unknown action")
        current = _lifecycle_from_row(row)
        if current.state is not _LifecycleState.EXECUTED_PENDING_VERIFICATION:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Verification requires an executed action")
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        target_state = _LifecycleState.VERIFIED_SUCCESS if result.success else _LifecycleState.VERIFICATION_FAILED
        validate_transition(current, target_state, now=now)
        advanced = advance_lifecycle(current, target_state, now=now)
        evidence = json.dumps(list(result.evidence_references), ensure_ascii=False, separators=(",", ":"))
        digest = _verification_integrity_digest(
            verification_id=result.verification_id,
            action_id=result.action_id,
            success=result.success,
            reason_code=result.reason_code.value,
            expected_revision=result.expected_revision,
            observed_revision=result.observed_revision,
            expected_digest=result.expected_digest,
            observed_digest=result.observed_digest,
            verifier=result.verifier,
            verification_version=result.verification_version,
            evidence_references=evidence,
            observed_at=result.observed_at.isoformat(),
        )
        try:
            with self._db.transaction():
                self._db.connection.execute(
                    "INSERT INTO verification_records (verification_id, action_id, success, reason_code, expected_revision,"
                    " observed_revision, expected_digest, observed_digest, verifier, verification_version,"
                    " evidence_references, observed_at, integrity_digest)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        result.verification_id,
                        result.action_id,
                        1 if result.success else 0,
                        result.reason_code.value,
                        result.expected_revision,
                        result.observed_revision,
                        result.expected_digest,
                        result.observed_digest,
                        result.verifier,
                        result.verification_version,
                        evidence,
                        result.observed_at.isoformat(),
                        digest,
                    ),
                )
                cursor = self._db.connection.execute(
                    "UPDATE actions SET lifecycle_state = ?, outcome = ?, version = ?, updated_at = ?"
                    " WHERE action_id = ? AND version = ? AND lifecycle_state = ?",
                    (
                        advanced.state.value,
                        "verification_succeeded" if result.success else "verification_failed",
                        advanced.version,
                        _utc(now).isoformat() if now is not None else self._now_iso(),
                        action_id,
                        expected_version,
                        current.state.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
                self._append_evidence(audit_drafts)
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Verification outcome cannot be persisted") from exc
        return advanced

    def mark_reconciled(self, action_id: str, expected_version: int, *, to_state, outcome: str, now, audit_drafts=()) -> ActionLifecycle:
        """CAS transition used by UNKNOWN_OUTCOME reconciliation."""

        row = self._db.connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise _corrupt("Unknown action")
        current = _lifecycle_from_row(row)
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        validate_transition(current, to_state, now=now)
        advanced = advance_lifecycle(current, to_state, now=now)
        try:
            with self._db.transaction():
                cursor = self._db.connection.execute(
                    "UPDATE actions SET lifecycle_state = ?, outcome = ?, version = ?, updated_at = ?"
                    " WHERE action_id = ? AND version = ? AND lifecycle_state = ?",
                    (
                        advanced.state.value,
                        outcome.value if hasattr(outcome, "value") else str(outcome),
                        advanced.version,
                        _utc(now).isoformat() if now is not None else self._now_iso(),
                        action_id,
                        expected_version,
                        current.state.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
                self._append_evidence(audit_drafts)
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Reconciliation cannot be persisted") from exc
        return advanced

    def advance_rollback_state(self, action_id: str, expected_version: int, *, from_states, to_state, now, audit_drafts=()) -> ActionLifecycle:
        """CAS the original action through the rollback verdict transitions."""

        row = self._db.connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise _corrupt("Unknown action")
        current = _lifecycle_from_row(row)
        if current.state not in from_states:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Action is not in a rollback-eligible state")
        if current.version != expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        validate_transition(current, to_state, now=now)
        advanced = advance_lifecycle(current, to_state, now=now)
        try:
            with self._db.transaction():
                cursor = self._db.connection.execute(
                    "UPDATE actions SET lifecycle_state = ?, version = ?, updated_at = ?"
                    " WHERE action_id = ? AND version = ? AND lifecycle_state = ?",
                    (
                        advanced.state.value,
                        advanced.version,
                        _utc(now).isoformat() if now is not None else self._now_iso(),
                        action_id,
                        expected_version,
                        current.state.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
                self._append_evidence(audit_drafts)
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Rollback state cannot be persisted") from exc
        return advanced

    def release_lease(self, action_id: str, *, lease_id: str, fencing_token: int, now) -> bool:
        """Release the CURRENT lease only; a stale release is refused.

        Classic fencing rule: lease A (token 7) releasing after lease B
        (token 8) was granted must NOT release B. The release matches on
        action_id AND lease_id AND fencing_token AND state='granted'.
        """

        from datetime import timedelta as _timedelta

        from aipm.control_plane.storage.sqlite_store import DEFAULT_LEASE_TTL

        del DEFAULT_LEASE_TTL  # explicit non-renewal documentation
        try:
            with self._db.transaction():
                cursor = self._db.connection.execute(
                    "UPDATE execution_leases SET state = 'released', released_at = ?"
                    " WHERE action_id = ? AND lease_id = ? AND fencing_token = ? AND state = 'granted'",
                    (
                        _utc(now).isoformat() if now is not None else self._now_iso(),
                        action_id,
                        lease_id,
                        fencing_token,
                    ),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Lease release cannot be persisted") from exc

    def bind_contract_evidence(self, action_id: str, *, expected_version: int, contract_version: str, capability_version: str, contract_digest: str, now) -> None:
        """Durably bind the contract digest to the action; immutable once set.

        Idempotent: binding the same digest again is a no-op (replay).
        A different digest for the same action is a conflict.
        """

        row = self._db.connection.execute(
            "SELECT contract_digest FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise _corrupt("Unknown action")
        if row["contract_digest"] is not None:
            if row["contract_digest"] == contract_digest:
                return  # replay: same contract, already bound
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Contract digest already bound to a different value")
        with self._db.transaction():
            self._db.connection.execute(
                "UPDATE actions SET contract_version = ?, capability_version = ?, contract_digest = ?"
                " WHERE action_id = ? AND version = ?",
                (contract_version, capability_version, contract_digest, action_id, expected_version),
            )

    def get_contract_evidence(self, action_id: str):
        row = self._db.connection.execute(
            "SELECT contract_version, capability_version, contract_digest FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "contract_version": row["contract_version"],
            "capability_version": row["capability_version"],
            "contract_digest": row["contract_digest"],
        }

    def outcome_for_action(self, action_id: str):
        row = self._db.connection.execute(
            "SELECT outcome FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            return None
        return row["outcome"]

    # -- confirmations ------------------------------------------------------

    def put_confirmation(self, binding) -> None:
        from aipm.control_plane.models import ConfirmationBinding

        if not isinstance(binding, ConfirmationBinding):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid confirmation binding")
        try:
            with self._db.transaction():
                self._upsert_confirmation(binding)
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Confirmation cannot be stored") from exc

    def put(self, binding) -> None:
        self.put_confirmation(binding)

    def _upsert_confirmation(self, binding) -> None:
        existing = self._db.connection.execute(
            "SELECT state FROM confirmations WHERE confirmation_id = ?",
            (binding.confirmation_id,),
        ).fetchone()
        if existing is not None and existing["state"] == ConfirmationState.CONSUMED.value:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Confirmation was already consumed")
        self._db.connection.execute(
            "INSERT OR REPLACE INTO confirmations (confirmation_id, decision_id, action_id, plan_id, plan_digest,"
            " target_revision, target_digest, policy_version, requester_subject, confirmed_by_subject,"
            " confirmation_kind, request_canonical, scope, state, created_at, expires_at, consumed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                binding.confirmation_id,
                binding.decision_id,
                binding.action_id,
                binding.plan_id,
                binding.plan_digest,
                binding.target_revision,
                binding.target_digest,
                binding.policy_version,
                binding.requester_subject,
                binding.confirmed_by_subject,
                binding.confirmation_kind.value,
                binding.request.canonical(),
                binding.scope,
                binding.state.value,
                binding.created_at.isoformat(),
                binding.expires_at.isoformat(),
                None,
            ),
        )

    def get_confirmation(self, confirmation_id: str):
        row = self._db.connection.execute(
            "SELECT * FROM confirmations WHERE confirmation_id = ?",
            (confirmation_id,),
        ).fetchone()
        if row is None:
            return None
        return _confirmation_from_row(row)

    def get(self, confirmation_id: str):
        return self.get_confirmation(confirmation_id)

    def get_active_for_action(self, action_id: str):
        row = self._db.connection.execute(
            "SELECT * FROM confirmations WHERE action_id = ? AND state = ?",
            (action_id, ConfirmationState.CONFIRMATION_REQUESTED.value),
        ).fetchone()
        if row is None:
            return None
        return _confirmation_from_row(row)

    def has_active_for_action(self, action_id: str) -> bool:
        rows = self._db.connection.execute(
            "SELECT state FROM confirmations WHERE action_id = ?",
            (action_id,),
        ).fetchall()
        return any(row["state"] in _ACTIVE_CONFIRMATION_STATES for row in rows)

    def count(self) -> int:
        row = self._db.connection.execute("SELECT COUNT(*) AS total FROM confirmations").fetchone()
        return int(row["total"]) if row else 0

    def as_mapping(self) -> Mapping:
        rows = self._db.connection.execute("SELECT * FROM confirmations").fetchall()
        return {row["confirmation_id"]: _confirmation_from_row(row) for row in rows}

    def record_confirmation_with_advance(self, binding, transition: LifecycleTransition, *, audit_drafts=()) -> ActionLifecycle:
        """Atomically persist the confirmed binding and apply the CAS transition."""

        from aipm.control_plane.models import ConfirmationBinding

        if not isinstance(binding, ConfirmationBinding):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid confirmation binding")
        if binding.action_id != transition.action_id:
            raise ControlPlaneError(PlanningErrorCode.CONFIRMATION_MISMATCH, "Confirmation does not match the transition target")
        row = self._db.connection.execute(
            "SELECT * FROM actions WHERE action_id = ?",
            (transition.action_id,),
        ).fetchone()
        if row is None:
            raise _corrupt("Unknown action")
        current = _lifecycle_from_row(row)
        validate_transition(current, transition.next_state, now=transition.now, actor_subject=transition.approver_subject)
        if current.version != transition.expected_version:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
        advanced = advance_lifecycle(current, transition.next_state, now=transition.now, actor_subject=transition.approver_subject)
        try:
            with self._db.transaction():
                self._upsert_confirmation(binding)
                cursor = self._db.connection.execute(
                    "UPDATE actions SET lifecycle_state = ?, approver_subject = ?, version = ?, updated_at = ?"
                    " WHERE action_id = ? AND version = ?",
                    (
                        advanced.state.value,
                        advanced.approver_subject,
                        advanced.version,
                        _utc(transition.now).isoformat() if transition.now is not None else self._now_iso(),
                        transition.action_id,
                        transition.expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Stale action version")
                self._append_evidence(audit_drafts)
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STATE_CONFLICT, "Confirmation transition cannot be persisted") from exc
        return advanced

    def _now_iso(self) -> str:
        return _utc(datetime.now(timezone.utc)).isoformat()


class SQLiteKillSwitchStore:
    """Durable kill-switch state; a missing record means engaged.

    With an attached audit sink, the state write and its evidence share one
    transaction.
    """

    __slots__ = ("_db", "_audit", "_initialized")

    def __init__(self, db: ControlPlaneDatabase, *, audit=None) -> None:
        if not isinstance(db, ControlPlaneDatabase):
            raise TypeError("SQLiteKillSwitchStore requires a ControlPlaneDatabase")
        if audit is not None and not hasattr(audit, "append_in_transaction"):
            raise TypeError("audit must provide append_in_transaction")
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "_audit", audit)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("SQLiteKillSwitchStore configuration is immutable")
        object.__setattr__(self, name, value)

    def record_for(self, environment):
        row = self._db.connection.execute(
            "SELECT * FROM kill_switch_state WHERE environment = ?",
            (environment.value if isinstance(environment, Environment) else str(environment),),
        ).fetchone()
        if row is None:
            return None
        return self._record_from_row(row)

    def save(self, switch, *, epoch: int, actor_subject: str | None, audit_drafts=()) -> None:
        try:
            with self._db.transaction():
                self._db.connection.execute(
                    "INSERT INTO kill_switch_state (environment, state, epoch, reason, actor_subject, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(environment) DO UPDATE SET state = excluded.state, epoch = excluded.epoch,"
                    " reason = excluded.reason, actor_subject = excluded.actor_subject, updated_at = excluded.updated_at",
                    (
                        switch.environment.value,
                        switch.state.value,
                        epoch,
                        switch.reason,
                        actor_subject,
                        switch.created_at.isoformat(),
                        switch.updated_at.isoformat(),
                    ),
                )
                if self._audit is not None and audit_drafts:
                    for draft in audit_drafts:
                        self._audit.append_in_transaction(draft)
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Kill-switch state cannot be stored") from exc

    def records(self) -> tuple:
        rows = self._db.connection.execute("SELECT * FROM kill_switch_state").fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    def _record_from_row(self, row: sqlite3.Row) -> KillSwitch:
        try:
            return KillSwitch(
                environment=Environment(row["environment"]),
                state=KillSwitchState(row["state"]),
                reason=row["reason"] or "",
                created_at=_parse_timestamp(row["created_at"], name="kill-switch timestamp"),
                updated_at=_parse_timestamp(row["updated_at"], name="kill-switch update timestamp"),
                epoch=int(row["epoch"]),
                actor_subject=row["actor_subject"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _corrupt("Stored kill-switch state cannot be reconstructed") from exc


class DurableSessionStore:
    """Durable operator session store; only session-id HASHES are persisted.

    The raw session identifier exists only in the client cookie and in the
    return value of :meth:`create`; the database stores SHA-256(token) plus
    the principal subject, epoch, and validity windows. Revocation sets
    ``revoked_at`` and survives restart. Implements the same SessionStore
    contract as the in-memory staging double.
    """

    __slots__ = ("_db", "_absolute_lifetime", "_inactivity_timeout", "_clock", "_auth_epoch", "_initialized")

    def __init__(
        self,
        db: ControlPlaneDatabase,
        *,
        clock=None,
        absolute_lifetime=None,
        inactivity_timeout=None,
        auth_epoch: int = 1,
    ) -> None:
        from datetime import timedelta

        if not isinstance(db, ControlPlaneDatabase):
            raise TypeError("DurableSessionStore requires a ControlPlaneDatabase")
        if absolute_lifetime is None:
            absolute_lifetime = timedelta(minutes=30)
        if inactivity_timeout is None:
            inactivity_timeout = timedelta(minutes=10)
        if absolute_lifetime <= timedelta(0) or inactivity_timeout <= timedelta(0):
            raise ValueError("session durations must be positive")
        if not isinstance(auth_epoch, int) or auth_epoch < 1:
            raise ValueError("invalid authentication epoch")
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "_absolute_lifetime", absolute_lifetime)
        object.__setattr__(self, "_inactivity_timeout", inactivity_timeout)
        object.__setattr__(self, "_clock", clock or (lambda: datetime.now(timezone.utc)))
        object.__setattr__(self, "_auth_epoch", auth_epoch)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("DurableSessionStore configuration is immutable")
        object.__setattr__(self, name, value)

    @property
    def auth_epoch(self) -> int:
        return self._auth_epoch

    @staticmethod
    def _hash_token(token: str) -> str:
        import hashlib

        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, *, principal, now=None) -> OwnerSession:
        from datetime import timedelta

        from aipm.control_plane.identity import OwnerPrincipal
        from aipm.control_plane.session import OwnerSession

        if not isinstance(principal, OwnerPrincipal):
            raise TypeError("sessions require a canonical OwnerPrincipal")
        current = _utc(now) if now is not None else _utc(self._clock())
        raw_token = secrets.token_urlsafe(32)
        csrf_client_token = self._hash_token(secrets.token_urlsafe(32))
        session = OwnerSession(
            session_id=raw_token,
            principal=principal,
            auth_epoch=self._auth_epoch,
            created_at=current,
            last_seen_at=current,
            expires_at=current + self._absolute_lifetime,
            inactivity_expires_at=current + self._inactivity_timeout,
            csrf_token=csrf_client_token,
        )
        with self._db.transaction():
            self._db.connection.execute(
                "INSERT INTO operator_sessions (session_id_hash, principal_subject, auth_epoch, created_at,"
                " expires_at, inactivity_expires_at, last_seen_at, revoked_at, csrf_token_hash, session_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    self._hash_token(raw_token),
                    principal.subject,
                    principal.auth_epoch,
                    session.created_at.isoformat(),
                    session.expires_at.isoformat(),
                    session.inactivity_expires_at.isoformat(),
                    session.last_seen_at.isoformat(),
                    session.csrf_token,
                    SESSION_VERSION_DEFAULT,
                ),
            )
        return session

    def get(self, session_id: str, *, now=None):
        from aipm.control_plane.identity import AuthenticationMethod, OwnerPrincipal, PrincipalVerification
        from aipm.control_plane.session import OwnerSession

        if not isinstance(session_id, str) or not session_id:
            return None
        current = _utc(now) if now is not None else _utc(self._clock())
        row = self._db.connection.execute(
            "SELECT * FROM operator_sessions WHERE session_id_hash = ?",
            (self._hash_token(session_id),),
        ).fetchone()
        if row is None:
            return None
        if row["revoked_at"] is not None or row["auth_epoch"] != self._auth_epoch:
            return None
        try:
            principal = OwnerPrincipal(
                subject=row["principal_subject"],
                issuer="aipm-owner-auth",
                authentication_method=AuthenticationMethod.ARGON2ID_OWNER_PASSPHRASE,
                verification=PrincipalVerification.VERIFIED,
                auth_epoch=int(row["auth_epoch"]),
                authenticated_at=_parse_timestamp(row["created_at"], name="session creation"),
                expires_at=_parse_timestamp(row["expires_at"], name="session expiry"),
                roles=("owner",),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _corrupt("Stored session cannot be reconstructed") from exc
        session = OwnerSession(
            session_id=session_id,
            principal=principal,
            auth_epoch=int(row["auth_epoch"]),
            created_at=_parse_timestamp(row["created_at"], name="session creation"),
            last_seen_at=current,
            expires_at=_parse_timestamp(row["expires_at"], name="session expiry"),
            inactivity_expires_at=min(
                _parse_timestamp(row["expires_at"], name="session expiry"),
                current + self._inactivity_timeout,
            ),
            csrf_token=row["csrf_token_hash"],
        )
        if not session.is_active(current):
            self.revoke(session_id, now=current)
            return None
        with self._db.transaction():
            self._db.connection.execute(
                "UPDATE operator_sessions SET last_seen_at = ? WHERE session_id_hash = ? AND revoked_at IS NULL",
                (current.isoformat(), self._hash_token(session_id)),
            )
        return session

    def revoke(self, session_id: str, *, now=None) -> None:
        if not isinstance(session_id, str) or not session_id:
            return
        current = _utc(now) if now is not None else _utc(self._clock())
        with self._db.transaction():
            self._db.connection.execute(
                "UPDATE operator_sessions SET revoked_at = ? WHERE session_id_hash = ? AND revoked_at IS NULL",
                (current.isoformat(), self._hash_token(session_id)),
            )

    def revoke_all(self, *, now=None) -> None:
        current = _utc(now) if now is not None else _utc(self._clock())
        with self._db.transaction():
            self._db.connection.execute(
                "UPDATE operator_sessions SET revoked_at = ? WHERE revoked_at IS NULL",
                (current.isoformat(),),
            )

    def rotate(self, session_id: str, *, now=None):
        current = _utc(now) if now is not None else _utc(self._clock())
        existing = self.get(session_id, now=current)
        if existing is None:
            return None
        self.revoke(session_id, now=current)
        return self.create(principal=existing.principal, now=current)

    def active_count(self, *, now=None) -> int:
        current = _utc(now) if now is not None else _utc(self._clock())
        row = self._db.connection.execute(
            "SELECT COUNT(*) AS total FROM operator_sessions WHERE revoked_at IS NULL AND auth_epoch = ?"
            " AND expires_at > ? AND inactivity_expires_at > ?",
            (self._auth_epoch, current.isoformat(), current.isoformat()),
        ).fetchone()
        return int(row["total"]) if row else 0

    def rotate_auth_epoch(self) -> int:
        """Advance the epoch and revoke every live session (durable)."""

        object.__setattr__(self, "_auth_epoch", self._auth_epoch + 1)
        current = _utc(self._clock())
        with self._db.transaction():
            self._db.connection.execute(
                "UPDATE operator_sessions SET revoked_at = ? WHERE revoked_at IS NULL",
                (current.isoformat(),),
            )
        return self._auth_epoch


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    """Future execution-lease record; not granted by any executor yet.

    Contract: ``fencing_token`` is monotonic per action and bound to the
    ``action_version`` the lease was granted against, so a stale executor
    holding an old token cannot commit progress against a newer action
    version. No code grants leases yet; the executor shot implements the
    grantor on top of this exact record.
    """

    lease_id: str
    action_id: str
    environment: str
    fencing_token: int
    state: str
    granted_at: datetime
    expires_at: datetime
    released_at: datetime | None = None
    holder: str | None = None
    action_version: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fencing_token, int) or self.fencing_token < 1:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid fencing token")
        if self.action_version is not None and (not isinstance(self.action_version, int) or self.action_version < 1):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid lease action version")


def _snapshot_integrity_digest(
    *,
    snapshot_id: str,
    action_id: str | None,
    plan_id: str | None,
    target_id: str,
    environment: str,
    revision: int,
    canonical_digest: str,
    payload_canonical: str,
    captured_at: str,
    snapshot_version: str,
) -> str:
    """Deterministic integrity digest over the canonical snapshot content."""

    payload = {
        "action_id": action_id,
        "canonical_digest": canonical_digest,
        "captured_at": captured_at,
        "environment": environment,
        "plan_id": plan_id,
        "payload_canonical": payload_canonical,
        "revision": revision,
        "snapshot_id": snapshot_id,
        "snapshot_version": snapshot_version,
        "target_id": target_id,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    """Immutable, integrity-protected before-image of a ProjectPlan revision.

    ``payload_canonical`` is the full canonical payload of the prior plan
    (mutable field values included), enough to reconstruct the exact prior
    state for rollback. The ``integrity_digest`` covers every field; loads
    that fail verification are rejected, never repaired.
    """

    snapshot_id: str
    target_id: str
    environment: str
    revision: int
    canonical_digest: str
    payload_canonical: str
    action_id: str | None
    captured_at: datetime
    plan_id: str | None = None
    snapshot_version: str = SNAPSHOT_VERSION_DEFAULT
    integrity_digest: str = ""

    def __post_init__(self) -> None:
        import re

        if self.snapshot_version != SNAPSHOT_VERSION_DEFAULT:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Unknown snapshot version")
        if not isinstance(self.revision, int) or self.revision < 1:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid snapshot revision")
        if not re.fullmatch(r"[0-9a-f]{64}", self.canonical_digest or ""):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid snapshot plan digest")
        expected = _snapshot_integrity_digest(
            snapshot_id=self.snapshot_id,
            action_id=self.action_id,
            plan_id=self.plan_id,
            target_id=self.target_id,
            environment=self.environment,
            revision=self.revision,
            canonical_digest=self.canonical_digest,
            payload_canonical=self.payload_canonical,
            captured_at=self.captured_at.isoformat() if isinstance(self.captured_at, datetime) else str(self.captured_at),
            snapshot_version=self.snapshot_version,
        )
        if not self.integrity_digest:
            object.__setattr__(self, "integrity_digest", expected)
        elif self.integrity_digest != expected:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Snapshot failed integrity verification")


def _verification_integrity_digest(
    *,
    verification_id: str,
    action_id: str,
    success: bool,
    reason_code: str,
    expected_revision: int,
    observed_revision: int | None,
    expected_digest: str,
    observed_digest: str | None,
    verifier: str,
    verification_version: str,
    evidence_references: str,
    observed_at: str,
) -> str:
    payload = {
        "action_id": action_id,
        "evidence_references": evidence_references,
        "expected_digest": expected_digest,
        "expected_revision": expected_revision,
        "observed_at": observed_at,
        "observed_digest": observed_digest,
        "observed_revision": observed_revision,
        "reason_code": reason_code,
        "success": success,
        "verification_id": verification_id,
        "verification_version": verification_version,
        "verifier": verifier,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredVerificationRecord:
    """Durable verification evidence; integrity-protected on load."""

    record: Any  # aipm.control_plane.verification.VerificationResult
    integrity_digest: str


class SQLiteVerificationRepository:
    """Durable, integrity-protected verification evidence."""

    __slots__ = ("_db", "_audit", "_initialized")

    def __init__(self, db: ControlPlaneDatabase, *, audit=None) -> None:
        if not isinstance(db, ControlPlaneDatabase):
            raise TypeError("SQLiteVerificationRepository requires a ControlPlaneDatabase")
        if audit is not None and not hasattr(audit, "append_in_transaction"):
            raise TypeError("audit must provide append_in_transaction")
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "_audit", audit)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("SQLiteVerificationRepository configuration is immutable")
        object.__setattr__(self, name, value)

    def _append_evidence(self, drafts) -> None:
        if not self._audit or not drafts:
            return
        for draft in drafts:
            self._audit.append_in_transaction(draft)

    def save(self, result, *, audit_drafts=()):
        from aipm.control_plane.verification import VerificationResult

        if not isinstance(result, VerificationResult):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid verification result")
        evidence = json.dumps(list(result.evidence_references), ensure_ascii=False, separators=(",", ":"))
        digest = _verification_integrity_digest(
            verification_id=result.verification_id,
            action_id=result.action_id,
            success=result.success,
            reason_code=result.reason_code.value,
            expected_revision=result.expected_revision,
            observed_revision=result.observed_revision,
            expected_digest=result.expected_digest,
            observed_digest=result.observed_digest,
            verifier=result.verifier,
            verification_version=result.verification_version,
            evidence_references=evidence,
            observed_at=result.observed_at.isoformat(),
        )
        try:
            with self._db.transaction():
                self._db.connection.execute(
                    "INSERT INTO verification_records (verification_id, action_id, success, reason_code, expected_revision,"
                    " observed_revision, expected_digest, observed_digest, verifier, verification_version,"
                    " evidence_references, observed_at, integrity_digest)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        result.verification_id,
                        result.action_id,
                        1 if result.success else 0,
                        result.reason_code.value,
                        result.expected_revision,
                        result.observed_revision,
                        result.expected_digest,
                        result.observed_digest,
                        result.verifier,
                        result.verification_version,
                        evidence,
                        result.observed_at.isoformat(),
                        digest,
                    ),
                )
                self._append_evidence(audit_drafts)
        except sqlite3.IntegrityError as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Verification record already exists") from exc
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Verification record cannot be stored") from exc
        return StoredVerificationRecord(record=result, integrity_digest=digest)

    def get(self, verification_id: str):
        row = self._db.connection.execute(
            "SELECT * FROM verification_records WHERE verification_id = ?",
            (verification_id,),
        ).fetchone()
        if row is None:
            return None
        return _verification_from_row(row)

    def records_for_action(self, action_id: str) -> tuple:
        rows = self._db.connection.execute(
            "SELECT verification_id FROM verification_records WHERE action_id = ? ORDER BY observed_at",
            (action_id,),
        ).fetchall()
        records = [self.get(row["verification_id"]) for row in rows]
        return tuple(record for record in records if record is not None)

    def count(self) -> int:
        row = self._db.connection.execute("SELECT COUNT(*) AS total FROM verification_records").fetchone()
        return int(row["total"]) if row else 0


def _verification_from_row(row: sqlite3.Row):
    from datetime import datetime

    from aipm.control_plane.verification import VerificationCode, VerificationResult

    try:
        result = VerificationResult(
            verification_id=row["verification_id"],
            action_id=row["action_id"],
            success=bool(row["success"]),
            reason_code=VerificationCode(row["reason_code"]),
            expected_revision=int(row["expected_revision"]),
            observed_revision=int(row["observed_revision"]) if row["observed_revision"] is not None else None,
            expected_digest=row["expected_digest"],
            observed_digest=row["observed_digest"],
            observed_at=_parse_timestamp(row["observed_at"], name="verification timestamp"),
            verifier=row["verifier"],
            verification_version=row["verification_version"],
            evidence_references=tuple(json.loads(row["evidence_references"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _corrupt("Stored verification record cannot be reconstructed") from exc
    expected = _verification_integrity_digest(
        verification_id=row["verification_id"],
        action_id=row["action_id"],
        success=bool(row["success"]),
        reason_code=row["reason_code"],
        expected_revision=int(row["expected_revision"]),
        observed_revision=int(row["observed_revision"]) if row["observed_revision"] is not None else None,
        expected_digest=row["expected_digest"],
        observed_digest=row["observed_digest"],
        verifier=row["verifier"],
        verification_version=row["verification_version"],
        evidence_references=row["evidence_references"],
        observed_at=row["observed_at"],
    )
    if row["integrity_digest"] != expected:
        raise _corrupt("Stored verification record failed integrity verification")
    return StoredVerificationRecord(record=result, integrity_digest=expected)


class SQLiteLeaseRepository:
    """Lease persistence scaffold; no executor exists to grant leases yet."""

    __slots__ = ("_db", "_initialized")

    def __init__(self, db: ControlPlaneDatabase) -> None:
        if not isinstance(db, ControlPlaneDatabase):
            raise TypeError("SQLiteLeaseRepository requires a ControlPlaneDatabase")
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("SQLiteLeaseRepository configuration is immutable")
        object.__setattr__(self, name, value)

    def save(self, lease: ExecutionLease) -> None:
        if not isinstance(lease, ExecutionLease):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid execution lease")
        try:
            with self._db.transaction():
                self._db.connection.execute(
                    "INSERT OR REPLACE INTO execution_leases (lease_id, action_id, environment, fencing_token, state, holder, granted_at, expires_at, released_at, action_version)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        lease.lease_id,
                        lease.action_id,
                        lease.environment,
                        lease.fencing_token,
                        lease.state,
                        lease.holder,
                        lease.granted_at.isoformat(),
                        lease.expires_at.isoformat(),
                        lease.released_at.isoformat() if lease.released_at else None,
                        lease.action_version,
                    ),
                )
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Execution lease cannot be stored") from exc

    def get(self, lease_id: str) -> ExecutionLease | None:
        row = self._db.connection.execute(
            "SELECT * FROM execution_leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return ExecutionLease(
                lease_id=row["lease_id"],
                action_id=row["action_id"],
                environment=row["environment"],
                fencing_token=int(row["fencing_token"]),
                state=row["state"],
                holder=row["holder"],
                granted_at=_parse_timestamp(row["granted_at"], name="lease timestamp"),
                expires_at=_parse_timestamp(row["expires_at"], name="lease expiry"),
                released_at=_parse_timestamp(row["released_at"], name="lease release") if row["released_at"] else None,
                action_version=int(row["action_version"]) if row["action_version"] is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _corrupt("Stored execution lease cannot be reconstructed") from exc

    def leases_for_action(self, action_id: str) -> tuple[ExecutionLease, ...]:
        rows = self._db.connection.execute(
            "SELECT lease_id FROM execution_leases WHERE action_id = ?",
            (action_id,),
        ).fetchall()
        leases = [self.get(row["lease_id"]) for row in rows]
        return tuple(lease for lease in leases if lease is not None)


class SQLitePlanSnapshotRepository:
    """Append-only before-image persistence; nothing is ever overwritten."""

    __slots__ = ("_db", "_initialized")

    def __init__(self, db: ControlPlaneDatabase) -> None:
        if not isinstance(db, ControlPlaneDatabase):
            raise TypeError("SQLitePlanSnapshotRepository requires a ControlPlaneDatabase")
        object.__setattr__(self, "_db", db)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("SQLitePlanSnapshotRepository configuration is immutable")
        object.__setattr__(self, name, value)

    def save(self, snapshot: PlanSnapshot) -> PlanSnapshot:
        if not isinstance(snapshot, PlanSnapshot):
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Invalid plan snapshot")
        try:
            with self._db.transaction():
                _insert_snapshot_row(self._db.connection, snapshot)
        except sqlite3.IntegrityError as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Snapshot identity already exists") from exc
        except sqlite3.Error as exc:
            raise ControlPlaneError(PlanningErrorCode.STORAGE_CORRUPT, "Plan snapshot cannot be stored") from exc
        return snapshot

    def get(self, snapshot_id: str) -> PlanSnapshot | None:
        row = self._db.connection.execute(
            "SELECT * FROM plan_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        return _snapshot_from_row(row)

    def snapshots_for_target(self, target_id: str) -> tuple[PlanSnapshot, ...]:
        rows = self._db.connection.execute(
            "SELECT snapshot_id FROM plan_snapshots WHERE target_id = ? ORDER BY captured_at",
            (target_id,),
        ).fetchall()
        snapshots = [self.get(row["snapshot_id"]) for row in rows]
        return tuple(snapshot for snapshot in snapshots if snapshot is not None)

    def snapshot_for_action(self, action_id: str) -> PlanSnapshot | None:
        row = self._db.connection.execute(
            "SELECT snapshot_id FROM plan_snapshots WHERE action_id = ? ORDER BY captured_at DESC LIMIT 1",
            (action_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get(row["snapshot_id"])


def _insert_snapshot_row(connection: sqlite3.Connection, snapshot: PlanSnapshot) -> None:
    connection.execute(
        "INSERT INTO plan_snapshots (snapshot_id, target_id, environment, revision, canonical_digest, payload_canonical,"
        " action_id, captured_at, plan_id, snapshot_version, integrity_digest)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot.snapshot_id,
            snapshot.target_id,
            snapshot.environment,
            snapshot.revision,
            snapshot.canonical_digest,
            snapshot.payload_canonical,
            snapshot.action_id,
            snapshot.captured_at.isoformat(),
            snapshot.plan_id,
            snapshot.snapshot_version,
            snapshot.integrity_digest,
        ),
    )


def _snapshot_from_row(row: sqlite3.Row) -> PlanSnapshot:
    try:
        return PlanSnapshot(
            snapshot_id=row["snapshot_id"],
            target_id=row["target_id"],
            environment=row["environment"],
            revision=int(row["revision"]),
            canonical_digest=row["canonical_digest"],
            payload_canonical=row["payload_canonical"],
            action_id=row["action_id"],
            captured_at=_parse_timestamp(row["captured_at"], name="snapshot timestamp"),
            plan_id=row["plan_id"],
            snapshot_version=row["snapshot_version"],
            integrity_digest=row["integrity_digest"],
        )
    except ControlPlaneError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _corrupt("Stored plan snapshot cannot be reconstructed") from exc
