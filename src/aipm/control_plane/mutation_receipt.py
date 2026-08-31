"""Durable mutation receipt: executor-side exactly-once-attempt fence.

The executor is the final mutation boundary. It must independently prove
whether a given (action_id, fencing_token) pair has already crossed the
external mutation boundary. The receipt is stored in a dedicated SQLite
table (in the executor's own database) with a UNIQUE constraint on
(action_id, fencing_token).

Semantics:
- RECEIPT_CREATED: the executor has claimed this mutation boundary and is
  about to invoke the provider. If the process dies at this point, the
  receipt proves that the mutation was ATTEMPTED but the outcome is UNKNOWN.
- MUTATION_SUCCEEDED: the provider returned success.
- MUTATION_FAILED: the provider returned a definitive failure.
- UNKNOWN_OUTCOME: the provider result could not be determined.

UNKNOWN_OUTCOME is NEVER automatically retried. The control plane must
reconcile through independent observation.
"""
from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from aipm.control_plane.audit.sanitize import AuditEventError, bounded_reference

MUTATION_RECEIPT_VERSION = "mc612-mutation-receipt-v1"


class MutationStatus(str, Enum):
    RECEIPT_CREATED = "receipt_created"
    MUTATION_SUCCEEDED = "mutation_succeeded"
    MUTATION_FAILED = "mutation_failed"
    UNKNOWN_OUTCOME = "unknown_outcome"


class MutationReceiptError(ValueError):
    """Raised when a mutation receipt operation fails."""


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    """Immutable durable record proving that a mutation boundary was crossed."""

    receipt_id: str
    action_id: str
    fencing_token: int
    capability_id: str
    target_id: str
    contract_digest: str
    mutation_status: MutationStatus
    provider_code: str
    created_at: str
    completed_at: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("receipt_id", self.receipt_id),
            ("action_id", self.action_id),
            ("capability_id", self.capability_id),
            ("target_id", self.target_id),
            ("contract_digest", self.contract_digest),
        ):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise MutationReceiptError(f"Invalid {name}")
        if not isinstance(self.fencing_token, int) or self.fencing_token < 1:
            raise MutationReceiptError("Invalid fencing token")

    def safe_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "action_id": self.action_id,
            "fencing_token": self.fencing_token,
            "capability_id": self.capability_id,
            "target_id": self.target_id,
            "contract_digest": self.contract_digest,
            "mutation_status": self.mutation_status.value,
            "provider_code": self.provider_code,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "version": MUTATION_RECEIPT_VERSION,
        }


_RECEIPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS executor_mutation_receipts (
    receipt_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    capability_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    contract_digest TEXT NOT NULL,
    mutation_status TEXT NOT NULL,
    provider_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT,
    receipt_version TEXT NOT NULL DEFAULT 'mc612-mutation-receipt-v1',
    UNIQUE (action_id, fencing_token)
)
"""


class MutationReceiptStore:
    """Durable, database-enforced mutation fence.

    The UNIQUE (action_id, fencing_token) constraint is the ultimate
    defense against duplicate external mutation. A second attempt with the
    same action and fencing token cannot create a second receipt, so the
    provider cannot be invoked twice.

    The store uses its own SQLite database (separate from the control-plane
    DB) to maintain independence. The executor service owns this database.

    Concurrency model: every operation opens its own short-lived SQLite
    connection with WAL + busy_timeout. claim() wraps its transaction in
    BEGIN IMMEDIATE so the SELECT-then-INSERT decision is serialized by
    SQLite's write lock; concurrent claims for the same (action_id,
    fencing_token) serialize, one INSERT commits, and every loser observes
    the winner's row before raising. Sharing one connection across threads
    would corrupt Python-level statement/transaction state even though
    sqlite3 itself is threadsafe, so no connection is ever shared here.
    """

    __slots__ = ("_db_path", "_initialized")

    def __init__(self, db_path: str | Path) -> None:
        db_file = Path(db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        object.__setattr__(self, "_db_path", db_file)
        object.__setattr__(self, "_initialized", True)
        conn = self._connect()
        try:
            conn.executescript(_RECEIPT_SCHEMA)
        finally:
            conn.close()

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("MutationReceiptStore configuration is immutable")
        object.__setattr__(self, name, value)

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh per-call connection (never shared between threads)."""
        conn = sqlite3.connect(str(self._db_path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def claim(self, *, action_id: str, fencing_token: int, capability_id: str, target_id: str, contract_digest: str, now: str | None = None) -> MutationReceipt:
        """Atomically claim the mutation boundary. Raises if already claimed."""
        moment = now or datetime.now(timezone.utc).isoformat()
        receipt = MutationReceipt(
            receipt_id=secrets.token_hex(16),
            action_id=action_id,
            fencing_token=fencing_token,
            capability_id=capability_id,
            target_id=target_id,
            contract_digest=contract_digest,
            mutation_status=MutationStatus.RECEIPT_CREATED,
            provider_code="",
            created_at=moment,
        )
        conn = self._connect()
        try:
            # BEGIN IMMEDIATE takes the write lock before the existence
            # check, serializing concurrent claims for the same identity.
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT mutation_status FROM executor_mutation_receipts WHERE action_id = ? AND fencing_token = ?",
                    (action_id, fencing_token),
                ).fetchone()
                if existing is not None:
                    conn.execute("ROLLBACK")
                    raise MutationReceiptError(
                        f"Mutation already claimed: action={action_id[:16]}, fence={fencing_token}, status={existing['mutation_status']}"
                    )
                conn.execute(
                    "INSERT INTO executor_mutation_receipts (receipt_id, action_id, fencing_token, capability_id, target_id, contract_digest, mutation_status, provider_code, created_at, receipt_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (receipt.receipt_id, receipt.action_id, receipt.fencing_token, receipt.capability_id, receipt.target_id, receipt.contract_digest, receipt.mutation_status.value, receipt.provider_code, receipt.created_at, MUTATION_RECEIPT_VERSION),
                )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                # Belt and braces: the write lock should already have
                # serialized this; treat any UNIQUE failure as a lost race.
                raise MutationReceiptError(
                    f"Mutation already claimed: action={action_id[:16]}, fence={fencing_token}, status=unknown"
                ) from None
            except sqlite3.OperationalError as exc:
                conn.execute("ROLLBACK")
                if "database is locked" in str(exc) or "database is busy" in str(exc):
                    raise MutationReceiptError(f"SQLite contention during claim: {exc}") from None
                raise
            except MutationReceiptError:
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
        except MutationReceiptError:
            raise
        except sqlite3.Error as exc:
            raise MutationReceiptError(f"SQLite claim failure: {exc}") from exc
        finally:
            conn.close()
        return receipt

    def complete(self, *, action_id: str, fencing_token: int, status: MutationStatus, provider_code: str, now: str | None = None) -> MutationReceipt:
        """Update the receipt with the provider outcome."""
        moment = now or datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    "UPDATE executor_mutation_receipts SET mutation_status = ?, provider_code = ?, completed_at = ?"
                    " WHERE action_id = ? AND fencing_token = ? AND mutation_status = ?",
                    (status.value, provider_code, moment, action_id, fencing_token, MutationStatus.RECEIPT_CREATED.value),
                )
                if cursor.rowcount != 1:
                    conn.execute("ROLLBACK")
                    raise MutationReceiptError("Receipt not found in RECEIPT_CREATED state")
                conn.execute("COMMIT")
            except MutationReceiptError:
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.Error as exc:
            raise MutationReceiptError(f"SQLite complete failure: {exc}") from exc
        finally:
            conn.close()
        return self.get(action_id=action_id, fencing_token=fencing_token)

    def get(self, *, action_id: str, fencing_token: int) -> MutationReceipt | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM executor_mutation_receipts WHERE action_id = ? AND fencing_token = ?",
                (action_id, fencing_token),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return MutationReceipt(
            receipt_id=row["receipt_id"],
            action_id=row["action_id"],
            fencing_token=row["fencing_token"],
            capability_id=row["capability_id"],
            target_id=row["target_id"],
            contract_digest=row["contract_digest"],
            mutation_status=MutationStatus(row["mutation_status"]),
            provider_code=row["provider_code"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    def count(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) AS total FROM executor_mutation_receipts").fetchone()
        finally:
            conn.close()
        return int(row["total"]) if row else 0
