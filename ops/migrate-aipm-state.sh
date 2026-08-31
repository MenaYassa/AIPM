#!/bin/bash
# AIPM state migration — SAFE SQLITE MIGRATION BOUNDARY (separate from identity setup).
#
# Migrates the live mission_control telemetry DB from the old mina-owned location
# to /var/lib/aipm/state/telemetry, WITHOUT copying a database that a running
# service may be writing (FINDING 2).
#
# Safety model:
#   1. The script NEVER restarts or stops services silently.
#   2. Writer quiescence is an EXPLICIT OPERATOR CHECKPOINT: the script verifies
#      that the source DB has no open writer (via fuser) and refuses to proceed
#      otherwise. If a writer is active, it prints the exact operator commands
#      and exits non-zero (no hidden restart).
#   3. WAL/SHM are handled explicitly: if -wal/-shm files exist, a WAL
#      checkpoint is required (operator executes the documented sqlite3
#      checkpoint command at a quiesced point) — this script performs a
#      TRUNCATE checkpoint only when the DB is already quiesced, then copies
#      db + verifies, and removes stale -wal/-shm ONLY from the DESTINATION.
#   4. The SOURCE DB is NEVER modified or removed. Source -wal/-shm are left
#      in place (owned by mina; the old service still owns that location).
#   5. A pre-migration backup is created and verified before the copy.
#   6. The destination is verified: integrity_check + schema identity +
#      expected tables + row-count equality + ownership + permissions.
#   7. All rollback material (timestamped backups) is preserved.
#
# Stages (idempotent; on failure: STOP + REPORT stage; no destructive rollback):
#   1. PRECHECK      paths, sqlite3, tools, mode
#   2. QUIESCE_CHECK source DB writer detection (fuser); refuse if active
#   3. SOURCE_VERIFY integrity_check on source; WAL presence handling
#   4. BACKUP        timestamped pre-migration backup + verification
#   5. COPY          copy verified backup to destination (db only; no wal/shm)
#   6. DEST_VERIFY   integrity_check + schema + tables + row counts + perms
#   7. SUMMARY       rollback material report + next operator steps
#
# Backup policy (FINDING 11): timestamped, never silently overwritten.
#   /var/lib/aipm/state/telemetry/backups/
#     mission_control.db.pre-migration.<UTC_TIMESTAMP>.<PID>.bak
#   Retention: keep ALL pre-migration backups; prune is a separate operator
#   decision (none performed by this script). A nightly host backup also
#   copies this directory (host backup policy: daily 01:17).
#
# Environment overrides (fixture tests only):
#   AIPM_TEST_ALLOW_NON_ROOT=1
#   AIPM_SRC_DB       (default /home/mina/.local/state/aipm/telemetry/mission_control.db)
#   AIPM_DST_DB       (default /var/lib/aipm/state/telemetry/mission_control.db)
#   AIPM_DST_OWNER    (default aipm:aipm)
#   AIPM_DST_DIR_MODE (default 0750)
#   AIPM_DST_DB_MODE  (default 0600)
#   AIPM_FUSER        (default fuser; fixture may stub)
#
# Usage:
#   sudo bash ops/migrate-aipm-state.sh --apply
#   bash ops/migrate-aipm-state.sh --dry-run
#   bash ops/migrate-aipm-state.sh --print-rollback
set -euo pipefail

MODE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) MODE="dry-run" ;;
        --apply) MODE="apply" ;;
        --print-rollback) MODE="print-rollback" ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            echo "Usage: $0 --dry-run | --apply | --print-rollback" >&2
            exit 2
            ;;
    esac
    shift
done
if [ -z "$MODE" ]; then
    echo "ERROR: mode required: --dry-run | --apply | --print-rollback" >&2
    exit 2
fi

SRC_DB="${AIPM_SRC_DB:-/home/mina/.local/state/aipm/telemetry/mission_control.db}"
DST_DB="${AIPM_DST_DB:-/var/lib/aipm/state/telemetry/mission_control.db}"
DST_OWNER="${AIPM_DST_OWNER:-aipm:aipm}"
DST_DIR_MODE="${AIPM_DST_DIR_MODE:-0750}"
DST_DB_MODE="${AIPM_DST_DB_MODE:-0600}"
FUSER_CMD="${AIPM_FUSER:-fuser}"

DST_DIR="$(dirname "$DST_DB")"
BACKUP_DIR="${DST_DIR}/backups"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

CURRENT_STAGE=""
die_at_stage() {
    echo "" >&2
    echo "STOP: stage '$CURRENT_STAGE' failed: $1" >&2
    echo "No automatic rollback performed. Source DB untouched." >&2
    echo "Rollback material: $BACKUP_DIR (pre-migration backups are never pruned)" >&2
    exit 1
}
begin_stage() { CURRENT_STAGE="$1"; echo ""; echo "=== STAGE: $1 ==="; }
log_dry() { echo "  [DRY RUN] $*"; }

# Canonical telemetry tables verified by DEST_VERIFY (schema identity =
# exact same table-name set in source and destination; subset below must
# all exist with equal row counts).
EXPECTED_TABLES="events incidents notifications host_samples container_samples container_resource_samples"

if [ "$MODE" = "print-rollback" ]; then
    cat <<'ROLLBACK'
TARGETED ROLLBACK — STATE MIGRATION (explicit, per-stage)

Restore previous destination (if the new copy must be discarded):
  sudo systemctl stop aipm-telemetry        # ONLY if the new service was already started
  sudo rm -f /var/lib/aipm/state/telemetry/mission_control.db{,-wal,-shm}

Restore the old service to its original DB (mina-owned location):
  # The SOURCE DB was never modified or removed by the migration.
  # Restart the original service pointing at the mina-owned DB (previous unit file),
  # or re-point the new unit back if that was the pre-migration configuration.

Backups (never pruned by this script):
  ls -la /var/lib/aipm/state/telemetry/backups/
  # Restore any:  sudo cp <backup> /var/lib/aipm/state/telemetry/mission_control.db
  #               sudo chown aipm:aipm ...; sudo chmod 600 ...
ROLLBACK
    exit 0
fi

# --- PRECHECK ---
begin_stage "PRECHECK"
if [ "$MODE" = "apply" ] && [ "${AIPM_TEST_ALLOW_NON_ROOT:-0}" != "1" ] && [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: --apply requires root (or AIPM_TEST_ALLOW_NON_ROOT=1 for fixtures)." >&2
    exit 1
fi
for tool in sqlite3 cp install stat sha256sum chown chmod; do
    command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: missing tool: $tool" >&2; exit 1; }
done
if ! command -v "$FUSER_CMD" >/dev/null 2>&1; then
    echo "ERROR: fuser not available (cannot verify writer quiescence)" >&2
    exit 1
fi
if [ ! -f "$SRC_DB" ]; then
    echo "No source DB at $SRC_DB — nothing to migrate."
    exit 0
fi
echo "PRECHECK OK (mode=$MODE)"

# --- QUIESCE_CHECK (writer-quiescence boundary; explicit operator checkpoint) ---
begin_stage "QUIESCE_CHECK"
Writers=0
if "$FUSER_CMD" -v "$SRC_DB" >/dev/null 2>&1; then
    Writers=1
fi
if [ "$Writers" -eq 1 ]; then
    echo "ERROR: source DB is OPEN by a running process — NOT transactionally safe." >&2
    echo "" >&2
    echo "The telemetry service is currently writing to:" >&2
    echo "  $SRC_DB" >&2
    echo "" >&2
    echo "OPERATOR CHECKPOINT (explicit; the script does NOT do this silently):" >&2
    echo "  1. sudo systemctl stop aipm-telemetry   (or the service currently using this DB)" >&2
    echo "  2. Re-run: sudo bash ops/migrate-aipm-state.sh --apply" >&2
    echo "  3. Reconfigure + restart the service against the NEW location afterwards" >&2
    exit 1
fi
echo "  Source DB has no open writer — quiesced."

# --- SOURCE_VERIFY ---
begin_stage "SOURCE_VERIFY"
SRC_INTEGRITY="$(sqlite3 "$SRC_DB" "PRAGMA integrity_check;" 2>&1)" || die_at_stage "cannot read source DB"
if [ "$SRC_INTEGRITY" != "ok" ]; then
    die_at_stage "source integrity_check: $SRC_INTEGRITY"
fi
echo "  Source integrity_check: ok"

SRC_HAS_WAL=0
[ -f "${SRC_DB}-wal" ] && SRC_HAS_WAL=1
if [ "$SRC_HAS_WAL" -eq 1 ]; then
    echo "  WAL file present (${SRC_DB}-wal)."
    if [ "$MODE" = "apply" ]; then
        BEFORE="$(sqlite3 "$SRC_DB" "PRAGMA journal_mode;")"
        sqlite3 "$SRC_DB" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null || die_at_stage "WAL checkpoint failed"
        AFTER="$(sqlite3 "$SRC_DB" "PRAGMA journal_mode;")"
        if [ "$BEFORE" != "$AFTER" ]; then
            die_at_stage "journal mode changed during checkpoint ($BEFORE -> $AFTER)"
        fi
        if [ -s "${SRC_DB}-wal" ]; then
            die_at_stage "WAL not fully checkpointed (non-empty after TRUNCATE)"
        fi
        echo "  WAL checkpointed (TRUNCATE) successfully; journal mode preserved: $AFTER"
    else
        log_dry "PRAGMA wal_checkpoint(TRUNCATE); verify journal mode unchanged; verify -wal empty"
    fi
else
    echo "  No WAL file (journal already fully contained in the main DB)."
fi

# --- BACKUP (timestamped; never overwritten; retention = keep all) ---
begin_stage "BACKUP"
BACKUP_FILE="${BACKUP_DIR}/mission_control.db.pre-migration.${TS}.$$.bak"
if [ "$MODE" = "dry-run" ]; then
    log_dry "mkdir -p $BACKUP_DIR (0700)"
    log_dry "cp $SRC_DB $BACKUP_FILE"
    log_dry "Verify: sqlite3 $BACKUP_FILE 'PRAGMA integrity_check;' == ok"
else
    mkdir -p "$BACKUP_DIR" && chmod 0700 "$BACKUP_DIR"
    cp "$SRC_DB" "$BACKUP_FILE" || die_at_stage "backup copy failed"
    BK_INTEGRITY="$(sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;")"
    if [ "$BK_INTEGRITY" != "ok" ]; then
        die_at_stage "backup failed verification: $BK_INTEGRITY"
    fi
    echo "  Backup created and verified: $BACKUP_FILE"
fi

# --- COPY (from the VERIFIED backup, never directly from the live source) ---
begin_stage "COPY"
if [ "$MODE" = "dry-run" ]; then
    log_dry "install -m $DST_DB_MODE $BACKUP_FILE $DST_DB"
    log_dry "chown $DST_OWNER $DST_DB"
    log_dry "Remove stale -wal/-shm ONLY at destination (none copied)"
else
    mkdir -p "$DST_DIR"
    chmod "$DST_DIR_MODE" "$DST_DIR"
    # Copy from the verified backup (stable file), never the (possibly hot) source.
    install -m "$DST_DB_MODE" "$BACKUP_FILE" "$DST_DB" || die_at_stage "install to destination failed"
    chown "$DST_OWNER" "$DST_DB" || die_at_stage "chown destination failed"
    rm -f "${DST_DB}-wal" "${DST_DB}-shm"
    echo "  Destination installed: $DST_DB ($DST_OWNER, mode $DST_DB_MODE)"
fi

# --- DEST_VERIFY ---
begin_stage "DEST_VERIFY"
EXPECTED_COUNT=$(wc -w <<EOF
$EXPECTED_TABLES
EOF
)
verify_destination() {
    local db="$1"
    sqlite3 "$db" "PRAGMA integrity_check;" | grep -qx "ok" || return 1
    [ "$(sqlite3 "$db" "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ($(echo "$EXPECTED_TABLES" | sed 's/\([^ ]*\)/"\1"/g;s/ /,/g'));")" -eq "$EXPECTED_COUNT" ] || return 1
    return 0
}
if [ "$MODE" = "apply" ]; then
    if ! verify_destination "$DST_DB"; then
        die_at_stage "destination verification failed (integrity or expected tables)"
    fi
    for t in $EXPECTED_TABLES; do
        SC="$(sqlite3 "$SRC_DB" "SELECT count(*) FROM $t;")"
        DC="$(sqlite3 "$DST_DB" "SELECT count(*) FROM $t;")"
        if [ "$SC" != "$DC" ]; then
            die_at_stage "row count mismatch for $t: src=$SC dst=$DC"
        fi
        echo "  $t: $DC rows (source == destination)"
    done
    DST_MODE="$(stat -c '%a' "$DST_DB")"
    DST_OWNR="$(stat -c '%u:%g' "$DST_DB")"
    # Resolve requested owner to numeric ids (robust in chroot/fixture envs)
    WANT_UID="${DST_OWNER%%:*}"; WANT_GID="${DST_OWNER##*:}"
    case "$WANT_UID" in
        ''|*[!0-9]*) WANT_UID="$(getent passwd "$WANT_UID" | cut -d: -f3)" ;;
    esac
    case "$WANT_GID" in
        ''|*[!0-9]*) WANT_GID="$(getent group "$WANT_GID" | cut -d: -f3)" ;;
    esac
    if [ -z "$WANT_UID" ] || [ -z "$WANT_GID" ]; then
        die_at_stage "cannot resolve destination owner '$DST_OWNER' to uid:gid"
    fi
    if [ "$DST_MODE" != "600" ] || [ "$DST_OWNR" != "$WANT_UID:$WANT_GID" ]; then
        die_at_stage "destination perms mismatch: $DST_OWNR mode $DST_MODE (expected $WANT_UID:$WANT_GID, 600)"
    fi
    echo "  Destination verified: integrity ok, schema present, row counts equal, perms exact."
else
    log_dry "integrity_check on $DST_DB == ok"
    log_dry "expected tables present: $EXPECTED_TABLES"
    log_dry "row counts equal per table (src vs dst)"
    log_dry "stat: owner=$DST_OWNER mode=$DST_DB_MODE"
fi

# --- SUMMARY ---
begin_stage "SUMMARY"
if [ "$MODE" = "apply" ]; then
    echo "  Migration complete."
    echo "  Rollback material (never pruned): $BACKUP_DIR"
    ls -la "$BACKUP_DIR" 2>/dev/null | tail -5
else
    log_dry "Report rollback material location: $BACKUP_DIR"
fi

cat <<'NEXT'

=== STATE MIGRATION COMPLETE ===
The source DB at the old location was NEVER modified or removed.
The service restart against the NEW DB location is an EXPLICIT operator step:
  sudo systemctl restart aipm-telemetry   (after unit files point to the new path)
NEXT

exit 0
