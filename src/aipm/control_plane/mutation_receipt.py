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

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
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
    """

    __slots__ = ("_db", "_lock", "_initialized")

    def __init__(self, db_path: str | Path) -> None:
        from pathlib import Path as _Path

        db_file = _Path(db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_file), check_same_thread=False)
        lock = threading.Lock()
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_RECEIPT_SCHEMA)
        conn.commit()
        object.__setattr__(self, "_db", conn)
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(self, "_initialized", True)

    def __setattr__(self, name, value):
        if getattr(self, "_initialized", False):
            raise AttributeError("MutationReceiptStore configuration is immutable")
        object.__setattr__(self, name, value)

    def claim(self, *, action_id: str, fencing_token: int, capability_id: str, target_id: str, contract_digest: str, now: str | None = None) -> MutationReceipt:
        """Atomically claim the mutation boundary. Raises if already claimed."""
        import secrets
        from datetime import datetime as _dt, timezone as _tz

        moment = now or _dt.now(_tz.utc).isoformat()
        receipt_id = secrets.token_hex(16)
        with self._lock:
            receipt = MutationReceipt(
                receipt_id=receipt_id,
                action_id=action_id,
                fencing_token=fencing_token,
                capability_id=capability_id,
                target_id=target_id,
                contract_digest=contract_digest,
                mutation_status=MutationStatus.RECEIPT_CREATED,
                provider_code="",
                created_at=moment,
            )
            try:
                with self._db:
                    self._db.execute(
                        "INSERT INTO executor_mutation_receipts (receipt_id, action_id, fencing_token, capability_id, target_id, contract_digest, mutation_status, provider_code, created_at, receipt_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (receipt.receipt_id, receipt.action_id, receipt.fencing_token, receipt.capability_id, receipt.target_id, receipt.contract_digest, receipt.mutation_status.value, receipt.provider_code, receipt.created_at, MUTATION_RECEIPT_VERSION),
                    )
            except sqlite3.IntegrityError:
                existing = self.get(action_id=action_id, fencing_token=fencing_token)
                if existing is not None:
                    raise MutationReceiptError(
                        f"Mutation already claimed: action={action_id[:16]}, fence={fencing_token}, status={existing.mutation_status.value}"
                    )
                raise
            except sqlite3.OperationalError as exc:
                if "database is locked" in str(exc) or "database is busy" in str(exc):
                    raise MutationReceiptError(f"SQLite contention during claim: {exc}")
                raise
        return receipt

    def complete(self, *, action_id: str, fencing_token: int, status: MutationStatus, provider_code: str, now: str | None = None) -> MutationReceipt:
        """Update the receipt with the provider outcome."""

        from datetime import datetime as _dt, timezone as _tz

        moment = now or _dt.now(_tz.utc).isoformat()
        with self._db:
            cursor = self._db.execute(
                "UPDATE executor_mutation_receipts SET mutation_status = ?, provider_code = ?, completed_at = ?"
                " WHERE action_id = ? AND fencing_token = ? AND mutation_status = ?",
                (status.value, provider_code, moment, action_id, fencing_token, MutationStatus.RECEIPT_CREATED.value),
            )
            if cursor.rowcount != 1:
                raise MutationReceiptError("Receipt not found in RECEIPT_CREATED state")
        return self.get(action_id=action_id, fencing_token=fencing_token)

    def get(self, *, action_id: str, fencing_token: int) -> MutationReceipt | None:
        row = self._db.execute(
            "SELECT * FROM executor_mutation_receipts WHERE action_id = ? AND fencing_token = ?",
            (action_id, fencing_token),
        ).fetchone()
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
        row = self._db.execute("SELECT COUNT(*) AS total FROM executor_mutation_receipts").fetchone()
        return int(row["total"]) if row else 0
