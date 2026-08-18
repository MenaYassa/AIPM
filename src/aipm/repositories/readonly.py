from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from aipm.core.exceptions import AIPMError


class ReadOnlyFilesystemError(AIPMError):
    """Raised when a dashboard database path is not protected against writes."""


def _writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


def require_read_only_filesystem(database_path: Path) -> None:
    """Require an existing database and a non-writable database directory.

    The dashboard must consume current WAL frames, so it cannot use SQLite's
    immutable mode. Instead, the service-level filesystem sandbox must deny
    writes to the database, existing WAL/SHM sidecars, and the containing
    directory. This validator fails closed before a read-only repository opens
    a connection when that boundary is absent.
    """

    database_path = database_path.expanduser()
    if not database_path.is_file():
        raise FileNotFoundError(f"Read-only database does not exist: {database_path}")
    directory = database_path.parent
    if not os.access(database_path, os.R_OK):
        raise ReadOnlyFilesystemError(f"Read-only database is not readable: {database_path}")
    if not os.access(directory, os.R_OK | os.X_OK):
        raise ReadOnlyFilesystemError(f"Read-only database directory is not accessible: {directory}")
    if _writable(database_path):
        raise ReadOnlyFilesystemError(f"Read-only database is writable: {database_path}")
    if _writable(directory):
        raise ReadOnlyFilesystemError(f"Read-only database directory is writable: {directory}")

    sidecars = {}
    for suffix in ("-wal", "-shm"):
        sidecar = database_path.with_name(database_path.name + suffix)
        sidecars[suffix] = sidecar
        if sidecar.exists():
            if not os.access(sidecar, os.R_OK):
                raise ReadOnlyFilesystemError(f"Read-only database sidecar is not readable: {sidecar}")
            if _writable(sidecar):
                raise ReadOnlyFilesystemError(f"Read-only database sidecar is writable: {sidecar}")

    try:
        with sqlite3.connect(f"{database_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True) as connection:
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    except sqlite3.DatabaseError as exc:
        raise ReadOnlyFilesystemError(f"Read-only database metadata is unavailable: {database_path}") from exc
    if journal_mode == "wal":
        for suffix, sidecar in sidecars.items():
            if not sidecar.is_file() or not os.access(sidecar, os.R_OK):
                raise ReadOnlyFilesystemError(f"WAL database requires readable pre-existing {suffix} sidecar: {sidecar}")
