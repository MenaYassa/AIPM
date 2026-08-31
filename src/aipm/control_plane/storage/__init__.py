"""Durable control-plane storage package.

Exposes the SQLite implementations of the storage contracts. The database is
the dedicated control-plane store only — it never touches telemetry, events,
incidents, or notification databases, and nothing in this package can execute
or mutate anything outside the control-plane database file.
"""

from aipm.control_plane.storage.schema import SCHEMA_NAME, SCHEMA_VERSION
from aipm.control_plane.storage.sqlite_store import (
    ControlPlaneDatabase,
    ControlPlaneStorageUnavailable,
    ExecutionLease,
    PlanSnapshot,
    SQLiteActionRepository,
    SQLiteKillSwitchStore,
    SQLiteLeaseRepository,
    SQLitePlanSnapshotRepository,
    SQLiteProjectPlanStore,
    SQLiteVerificationRepository,
    DurableSessionStore,
    StoredVerificationRecord,
    default_database_path,
)

__all__ = [
    "ControlPlaneDatabase",
    "ControlPlaneStorageUnavailable",
    "ExecutionLease",
    "PlanSnapshot",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "SQLiteActionRepository",
    "SQLiteKillSwitchStore",
    "SQLiteLeaseRepository",
    "SQLitePlanSnapshotRepository",
    "SQLiteProjectPlanStore",
    "SQLiteVerificationRepository",
    "DurableSessionStore",
    "StoredVerificationRecord",
    "default_database_path",
]
