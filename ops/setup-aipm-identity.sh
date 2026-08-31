#!/bin/bash
# AIPM dedicated identity setup — STAGE-BASED, IDEMPOTENT, NON-MUTATING IN DRY RUN.
#
# Scope (deliberately EXCLUDED from this script):
#   - database migration           -> ops/migrate-aipm-state.sh
#   - systemd unit installation   -> documented operator checkpoint
#   - any service restart          -> documented operator checkpoint
#
# Stages (each idempotent; on failure: STOP + REPORT stage, no auto-rollback):
#   1. PRECHECK       tool availability, root check (apply mode)
#   2. BACKUP         preserve current sudoers rule + group/user facts (report only)
#   3. IDENTITY       groups aipm, aipm-executor, aipm-runtime (created only if
#                     absent, otherwise REUSED); users aipm, aipm-executor with
#                     EXPLICIT primary group (--gid, never --user-group); an
#                     existing user is never recreated — it is verified
#                     (primary group + nologin shell), failing closed on drift
#   4. RUNTIME_DIRS   /var/lib/aipm tree + /var/lib/aipm-executor (state, logs)
#   5. PERMISSIONS    deterministic read-only runtime access (see table below)
#   6. SUDOERS        transactional candidate -> validate -> backup -> atomic install
#   7. VERIFY         report final state; print operator checkpoints (no restarts)
#
# Permission model (deterministic; no blanket recursive chmod):
#   target                      owner              group            mode
#   /var/lib/aipm               aipm               aipm             0700
#   /var/lib/aipm/state         aipm               aipm             0750
#   /var/lib/aipm/state/telemetry aipm             aipm             0750
#   /var/lib/aipm/logs          aipm               aipm             0750
#   /var/lib/aipm/executor      aipm-executor      aipm-executor    0750
#   app source (src/)           unchanged owner    aipm-runtime     dirs 0750 files 0640
#   venv (.venv/)               unchanged owner    aipm-runtime     dirs 0750 files 0640
#   venv bin/*                  unchanged owner    aipm-runtime     0750 (executables)
#   config/aipm.yaml            unchanged owner    aipm-runtime     0640
#
# Group rules (privilege model):
#   aipm           member of: aipm-executor (socket IPC), aipm-runtime (read-only code)
#   aipm-executor  member of: aipm-runtime (read-only code)
#   aipm-runtime grants: read/execute on immutable application runtime ONLY.
#   aipm-runtime grants: NO sudo, NO docker, NO write to source/DB/state.
#   The executor NEVER joins the aipm group (no access to control-plane DB).
#
# Environment overrides (for fixture tests; never needed on the VPS):
#   AIPM_TEST_ALLOW_NON_ROOT=1   skip root check (fixture tests only)
#   AIPM_APP_CODE                app source root        (default /home/ubuntu/aipm)
#   AIPM_HOME                    control-plane home     (default /var/lib/aipm)
#   AIPM_EXECUTOR_HOME           executor home          (default /var/lib/aipm-executor)
#   AIPM_SUDOERS_DIR             sudoers drop-in dir    (default /etc/sudoers.d)
#   AIPM_SUDOERS_DIR_MODE        sudoers drop-in mode   (default 0755, host uses 0755)
#   AIPM_SUDOERS_VALIDATE        sudoers validator cmd  (default: visudo)
#
# Usage:
#   sudo bash ops/setup-aipm-identity.sh --apply
#   bash ops/setup-aipm-identity.sh --dry-run
#   bash ops/setup-aipm-identity.sh --print-rollback   (read-only procedure)
set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing (mode REQUIRED — no implicit mutation)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Paths / identities
# ---------------------------------------------------------------------------
APP_CODE="${AIPM_APP_CODE:-/home/ubuntu/aipm}"
AIPM_HOME="${AIPM_HOME:-/var/lib/aipm}"
EXECUTOR_HOME="${AIPM_EXECUTOR_HOME:-/var/lib/aipm-executor}"
AIPM_STATE_DIR="${AIPM_HOME}/state"
AIPM_DB_DIR="${AIPM_STATE_DIR}/telemetry"
AIPM_LOG_DIR="${AIPM_HOME}/logs"
AIPM_EXECUTOR_STATE_DIR="${EXECUTOR_HOME}/state"
AIPM_EXECUTOR_LOG_DIR="${EXECUTOR_HOME}/logs"
SUDOERS_DIR="${AIPM_SUDOERS_DIR:-/etc/sudoers.d}"
SUDOERS_FILE="${SUDOERS_DIR}/aipm-systemd-restart"
SUDOERS_LEGACY_MINA="${SUDOERS_DIR}/aipm-systemd-restart-mina"
SUDOERS_BACKUP_DIR="${SUDOERS_DIR}/.aipm-backup"
VISUDO_CMD="${AIPM_SUDOERS_VALIDATE:-visudo}"

AIPM_USER="aipm"
AIPM_GROUP="aipm"
EXECUTOR_USER="aipm-executor"
EXECUTOR_GROUP="aipm-executor"
RUNTIME_GROUP="aipm-runtime"

SUDOERS_RULE="${EXECUTOR_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service"

CURRENT_STAGE=""
FAILED_STAGE=""

die_at_stage() {
    # Non-destructive failure: report exact failed stage and stop.
    FAILED_STAGE="$CURRENT_STAGE"
    echo "" >&2
    echo "STOP: stage '$CURRENT_STAGE' failed: $1" >&2
    echo "No automatic rollback performed." >&2
    echo "Re-run with --dry-run to inspect, or --print-rollback for targeted rollback." >&2
    exit 1
}

begin_stage() {
    CURRENT_STAGE="$1"
    echo ""
    echo "=== STAGE: $1 ==="
}

log_dry() {
    echo "  [DRY RUN] $*"
}

# ---------------------------------------------------------------------------
# print-rollback mode (read-only; prints targeted per-stage procedure)
# ---------------------------------------------------------------------------
if [ "$MODE" = "print-rollback" ]; then
    cat <<'ROLLBACK'
TARGETED ROLLBACK PROCEDURE (run stages in reverse; each step is explicit)

SUDOERS:
  # Restore the exact previous AIPM rule from the timestamped backup:
  sudo ls -la /etc/sudoers.d/.aipm-backup/
  sudo install -m 440 /etc/sudoers.d/.aipm-backup/aipm-systemd-restart.<UTC_TIMESTAMP> \
      /etc/sudoers.d/aipm-systemd-restart
  sudo visudo -c
  # If NO AIPM rule existed before: remove only the AIPM file:
  sudo rm -f /etc/sudoers.d/aipm-systemd-restart && sudo visudo -c
  # /etc/sudoers and unrelated /etc/sudoers.d/* are NEVER touched.

PERMISSIONS:
  # Revoke runtime read access (harmless; identities keep existing):
  sudo chgrp -R root /home/ubuntu/aipm/.venv /home/ubuntu/aipm/src 2>/dev/null
  sudo chmod 600 /home/ubuntu/aipm/config/aipm.yaml 2>/dev/null || true

RUNTIME_DIRS:
  # Only after confirming no service writes there:
  sudo rm -rf /var/lib/aipm/state /var/lib/aipm/logs /var/lib/aipm-executor/state

IDENTITY:
  # Only after verifying no files are owned by these identities:
  sudo userdel aipm-executor && sudo groupdel aipm-executor
  sudo userdel aipm && sudo groupdel aipm
  sudo groupdel aipm-runtime
ROLLBACK
    exit 0
fi

# ---------------------------------------------------------------------------
# PRECHECK
# ---------------------------------------------------------------------------
begin_stage "PRECHECK"

if [ "$MODE" = "apply" ] && [ "${AIPM_TEST_ALLOW_NON_ROOT:-0}" != "1" ]; then
    if [ "$(id -u)" -ne 0 ]; then
        echo "ERROR: --apply requires root. Use --dry-run for a non-mutating report." >&2
        exit 1
    fi
fi

for tool in useradd groupadd usermod chmod chown install mktemp sha256sum; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: required tool not found: $tool" >&2
        exit 1
    fi
done
if ! command -v "$VISUDO_CMD" >/dev/null 2>&1; then
    echo "ERROR: sudoers validator not found: $VISUDO_CMD" >&2
    exit 1
fi
if [ ! -d "$APP_CODE" ]; then
    echo "ERROR: AIPM application code not found at $APP_CODE" >&2
    exit 1
fi
echo "PRECHECK OK (mode=$MODE)"
[ "$MODE" = "dry-run" ] && echo "Dry run: NO changes will be made. Mutating commands are printed only."

# ---------------------------------------------------------------------------
# BACKUP (capture current state for rollback material; dry-run: report only)
# ---------------------------------------------------------------------------
begin_stage "BACKUP"

if [ "$MODE" = "apply" ]; then
    if [ -f "$SUDOERS_FILE" ]; then
        mkdir -p "$SUDOERS_BACKUP_DIR"
        chmod 700 "$SUDOERS_BACKUP_DIR"
        TS="$(date -u +%Y%m%dT%H%M%SZ)"
        cp -p "$SUDOERS_FILE" "$SUDOERS_BACKUP_DIR/aipm-systemd-restart.${TS}.$$"
        chmod 440 "$SUDOERS_BACKUP_DIR/aipm-systemd-restart.${TS}.$$"
        echo "  Backed up existing sudoers rule -> $SUDOERS_BACKUP_DIR/aipm-systemd-restart.$TS"
    else
        echo "  No existing AIPM sudoers rule (nothing to back up)."
    fi
else
    [ -f "$SUDOERS_FILE" ] && log_dry "Would back up $SUDOERS_FILE to $SUDOERS_BACKUP_DIR/" \
        || log_dry "No existing sudoers rule to back up"
fi

# ---------------------------------------------------------------------------
# IDENTITY
# ---------------------------------------------------------------------------
begin_stage "IDENTITY"

create_group_if_missing() {
    local name="$1"
    if getent group "$name" >/dev/null 2>&1; then
        echo "  Group $name exists (GID $(getent group "$name" | cut -d: -f3)); reusing it"
    elif [ "$MODE" = "dry-run" ]; then
        log_dry "groupadd --system $name"
    else
        groupadd --system "$name"
        echo "  Created group $name"
    fi
}

user_shell() {
    getent passwd "$1" | cut -d: -f7
}

user_primary_group() {
    # Resolve the user's primary group NAME from the passwd GID field.
    local gid
    gid="$(getent passwd "$1" | cut -d: -f4)"
    getent group "$gid" | cut -d: -f1
}

ensure_user_with_primary_group() {
    # Idempotent user creation with an EXPLICIT primary group.
    # Never uses --user-group (that fails when the same-named group already
    # exists — the exact Checkpoint-1 failure mode). Never recreates an
    # existing user: verifies primary group + shell instead, failing closed.
    local name="$1" home="$2" primary="$3"
    if getent passwd "$name" >/dev/null 2>&1; then
        local pgid shell_now
        pgid="$(user_primary_group "$name")"
        shell_now="$(user_shell "$name")"
        if [ "$pgid" != "$primary" ]; then
            die_at_stage "user $name exists with primary group '$pgid', expected '$primary' (no recreation; repair deliberately)"
        fi
        if [ "$shell_now" != "/usr/sbin/nologin" ]; then
            die_at_stage "user $name exists with shell '$shell_now', expected '/usr/sbin/nologin' (no recreation; repair deliberately)"
        fi
        echo "  User $name exists (UID $(id -u "$name")); primary group '$pgid', shell OK"
    elif [ "$MODE" = "dry-run" ]; then
        log_dry "useradd --system --home-dir $home --shell /usr/sbin/nologin --no-create-home --gid $primary $name"
    else
        if ! useradd --system --home-dir "$home" --shell /usr/sbin/nologin \
                --no-create-home --gid "$primary" "$name"; then
            die_at_stage "useradd failed for $name (primary group $primary)"
        fi
        # Fail closed: re-read what the OS actually recorded.
        local pgid shell_now
        pgid="$(user_primary_group "$name")"
        shell_now="$(user_shell "$name")"
        if [ "$pgid" != "$primary" ] || [ "$shell_now" != "/usr/sbin/nologin" ]; then
            die_at_stage "post-create verification failed for $name (primary group '$pgid', shell '$shell_now')"
        fi
        echo "  Created user $name (UID $(id -u "$name")), primary group '$primary'"
    fi
}

create_group_if_missing "$AIPM_GROUP"
create_group_if_missing "$EXECUTOR_GROUP"
create_group_if_missing "$RUNTIME_GROUP"
ensure_user_with_primary_group "$AIPM_USER" "$AIPM_HOME" "$AIPM_GROUP"
ensure_user_with_primary_group "$EXECUTOR_USER" "$EXECUTOR_HOME" "$EXECUTOR_GROUP"

# Membership model (see header). usermod -a preserves existing groups.
if [ "$MODE" = "apply" ]; then
    for membership in "$AIPM_USER:$EXECUTOR_GROUP" "$AIPM_USER:$RUNTIME_GROUP" "$EXECUTOR_USER:$RUNTIME_GROUP"; do
        member="${membership%%:*}"; group="${membership##*:}"
        if id -Gn "$member" 2>/dev/null | grep -qw "$group"; then
            echo "  $member already in $group"
        else
            usermod -a -G "$group" "$member"
            echo "  Added $member to $group"
        fi
    done
    # Privilege guard: neither identity may hold privileged groups.
    for name in "$AIPM_USER" "$EXECUTOR_USER"; do
        if id -Gn "$name" 2>/dev/null | grep -qE '(^| )(sudo|docker|admin|root|wheel)( |$)'; then
            die_at_stage "$name is in a privileged group"
        fi
        if [ "$(user_shell "$name")" != "/usr/sbin/nologin" ]; then
            die_at_stage "$name shell is not /usr/sbin/nologin"
        fi
        if [ "$(user_primary_group "$name")" != "$name" ]; then
            die_at_stage "$name primary group is not $name"
        fi
    done
    # Guard: aipm-runtime must contain ONLY the two service identities.
    for member in $(getent group "$RUNTIME_GROUP" | cut -d: -f4 | tr ',' ' '); do
        [ -z "$member" ] && continue
        if [ "$member" != "$AIPM_USER" ] && [ "$member" != "$EXECUTOR_USER" ]; then
            die_at_stage "unexpected member '$member' in $RUNTIME_GROUP"
        fi
    done
else
    log_dry "usermod -a -G $EXECUTOR_GROUP $AIPM_USER"
    log_dry "usermod -a -G $RUNTIME_GROUP $AIPM_USER"
    log_dry "usermod -a -G $RUNTIME_GROUP $EXECUTOR_USER"
    log_dry "Verify: no sudo/docker/admin/root membership for $AIPM_USER, $EXECUTOR_USER"
    log_dry "Verify: $RUNTIME_GROUP contains only $AIPM_USER, $EXECUTOR_USER"
fi

# ---------------------------------------------------------------------------
# RUNTIME_DIRS
# ---------------------------------------------------------------------------
begin_stage "RUNTIME_DIRS"

# Ownership targets are applied in PERMISSIONS; here only directory creation.
if [ "$MODE" = "dry-run" ]; then
    log_dry "mkdir -p $AIPM_STATE_DIR $AIPM_DB_DIR $AIPM_LOG_DIR"
    log_dry "mkdir -p $AIPM_EXECUTOR_STATE_DIR $AIPM_EXECUTOR_LOG_DIR"
    log_dry "mkdir -p $SUDOERS_BACKUP_DIR (mode 0700)"
else
    mkdir -p "$AIPM_STATE_DIR" "$AIPM_DB_DIR" "$AIPM_LOG_DIR"
    mkdir -p "$AIPM_EXECUTOR_STATE_DIR" "$AIPM_EXECUTOR_LOG_DIR"
    echo "  Runtime directories present."
fi

# ---------------------------------------------------------------------------
# PERMISSIONS (deterministic model — see header table)
# ---------------------------------------------------------------------------
begin_stage "PERMISSIONS"

if [ "$MODE" = "dry-run" ]; then
    log_dry "chown $AIPM_USER:$AIPM_GROUP $AIPM_HOME (single dir, no recursion)"
    log_dry "chmod 0700 $AIPM_HOME"
    log_dry "chown -R $AIPM_USER:$AIPM_GROUP $AIPM_STATE_DIR $AIPM_LOG_DIR  (bounded: control-plane state/logs only)"
    log_dry "chmod 0750 $AIPM_STATE_DIR $AIPM_DB_DIR $AIPM_LOG_DIR"
    log_dry "chown -R $EXECUTOR_USER:$EXECUTOR_GROUP $EXECUTOR_HOME  (bounded: executor home only)"
    log_dry "chmod 0750 $AIPM_EXECUTOR_STATE_DIR $AIPM_EXECUTOR_LOG_DIR"
    log_dry "chgrp -R $RUNTIME_GROUP $APP_CODE/src $APP_CODE/.venv  (read-only runtime group; owner unchanged)"
    log_dry "find $APP_CODE/src -type d -exec chmod 0750 {} +  (traversal)"
    log_dry "find $APP_CODE/src -type f -exec chmod 0640 {} +  (read)"
    log_dry "find $APP_CODE/.venv -type d -exec chmod 0750 {} +  (traversal)"
    log_dry "find $APP_CODE/.venv -type f ! -path '*/bin/*' -exec chmod 0640 {} +  (read)"
    log_dry "find $APP_CODE/.venv/bin -maxdepth 1 -type f -exec chmod 0750 {} +  (execute entry points)"
    log_dry "chgrp $RUNTIME_GROUP $APP_CODE/config/aipm.yaml; chmod 0640 $APP_CODE/config/aipm.yaml"
    log_dry "Config dirs must grant traversal: config/ 0750"
else
    # Control-plane home: single-directory ownership (NOT recursive across users' files).
    chown "$AIPM_USER:$AIPM_GROUP" "$AIPM_HOME"
    chmod 0700 "$AIPM_HOME"
    # Bounded recursion: control-plane state and logs (created above, owned by us).
    chown -R "$AIPM_USER:$AIPM_GROUP" "$AIPM_STATE_DIR" "$AIPM_LOG_DIR"
    chmod 0750 "$AIPM_STATE_DIR" "$AIPM_DB_DIR" "$AIPM_LOG_DIR"
    # Bounded recursion: dedicated executor home only.
    chown -R "$EXECUTOR_USER:$EXECUTOR_GROUP" "$EXECUTOR_HOME"
    chmod 0750 "$AIPM_EXECUTOR_STATE_DIR" "$AIPM_EXECUTOR_LOG_DIR"

    # Application runtime: read/execute via aipm-runtime. Owner is NEVER changed,
    # so the deployment operator retains exclusive write access.
    if [ -d "$APP_CODE/src" ]; then
        chgrp -R "$RUNTIME_GROUP" "$APP_CODE/src"
        find "$APP_CODE/src" -type d -exec chmod 0750 {} +
        find "$APP_CODE/src" -type f -exec chmod 0640 {} +
    else
        die_at_stage "missing application source: $APP_CODE/src"
    fi
    if [ -d "$APP_CODE/.venv" ]; then
        chgrp -R "$RUNTIME_GROUP" "$APP_CODE/.venv"
        find "$APP_CODE/.venv" -type d -exec chmod 0750 {} +
        find "$APP_CODE/.venv" -type f ! -path "$APP_CODE/.venv/bin/*" -exec chmod 0640 {} +
        if [ -d "$APP_CODE/.venv/bin" ]; then
            find "$APP_CODE/.venv/bin" -maxdepth 1 -type f -exec chmod 0750 {} +
        else
            die_at_stage "missing venv bin directory: $APP_CODE/.venv/bin"
        fi
    else
        die_at_stage "missing virtualenv: $APP_CODE/.venv"
    fi
    if [ -f "$APP_CODE/config/aipm.yaml" ]; then
        chgrp "$RUNTIME_GROUP" "$APP_CODE/config/aipm.yaml"
        chmod 0640 "$APP_CODE/config/aipm.yaml"
    fi
    if [ -d "$APP_CODE/config" ]; then
        chmod 0750 "$APP_CODE/config"
    fi
    echo "  Permission model applied."
fi

# ---------------------------------------------------------------------------
# SUDOERS (transactional: candidate -> validate -> backup -> atomic install)
# ---------------------------------------------------------------------------
begin_stage "SUDOERS"

if [ "$MODE" = "dry-run" ]; then
    log_dry "Write candidate file containing: $SUDOERS_RULE"
    log_dry "chmod 440 candidate; $VISUDO_CMD -cf candidate"
    log_dry "On success: install candidate -> $SUDOERS_FILE (atomic mv on same filesystem)"
    log_dry "On failure: delete ONLY the candidate; existing rule untouched"
    log_dry "Legacy mina rule ($SUDOERS_LEGACY_MINA), if present, is moved into backup (not deleted)"
else
    CANDIDATE="$(mktemp "${TMPDIR:-/tmp}/aipm-sudoers-candidate.XXXXXX")"
    NEW_TARGET="${SUDOERS_FILE}.new.$$"
    cleanup_sudoers() {
        rm -f "$CANDIDATE"
        rm -f "$NEW_TARGET"
    }
    trap cleanup_sudoers EXIT

    printf '%s\n' "$SUDOERS_RULE" > "$CANDIDATE"
    chmod 440 "$CANDIDATE"

    if ! "$VISUDO_CMD" -cf "$CANDIDATE" >/dev/null 2>&1; then
        cleanup_sudoers
        die_at_stage "candidate sudoers rule failed validation (existing rule untouched)"
    fi

    if [ -f "$SUDOERS_FILE" ] && [ "$(cat "$SUDOERS_FILE")" = "$SUDOERS_RULE" ]; then
        echo "  Rule already correct and validated; no change."
        cleanup_sudoers
    else
        install -m 440 "$CANDIDATE" "$NEW_TARGET"
        mv -f "$NEW_TARGET" "$SUDOERS_FILE"   # atomic rename, same filesystem
        echo "  Installed AIPM sudoers rule (atomic replace)."
    fi

    if ! "$VISUDO_CMD" -c >/dev/null 2>&1; then
        die_at_stage "post-install visudo -c failed (previous rule available in $SUDOERS_BACKUP_DIR)"
    fi

    # Legacy mina rule: preserve as backup material rather than deleting outright.
    if [ -f "$SUDOERS_LEGACY_MINA" ]; then
        mkdir -p "$SUDOERS_BACKUP_DIR"
        chmod 700 "$SUDOERS_BACKUP_DIR"
        TS2="$(date -u +%Y%m%dT%H%M%SZ)"
        mv "$SUDOERS_LEGACY_MINA" "$SUDOERS_BACKUP_DIR/aipm-systemd-restart-mina.$TS2"
        echo "  Moved legacy mina rule to backup: $SUDOERS_BACKUP_DIR/aipm-systemd-restart-mina.$TS2"
    fi

    echo "  Sudoers transaction complete."
fi

# ---------------------------------------------------------------------------
# VERIFY
# ---------------------------------------------------------------------------
begin_stage "VERIFY"

echo "Identity summary:"
if getent passwd "$AIPM_USER" >/dev/null 2>&1; then
    echo "  $AIPM_USER: UID $(id -u "$AIPM_USER"), shell $(user_shell "$AIPM_USER"), primary group $(user_primary_group "$AIPM_USER")"
    echo "    groups: $(id -Gn "$AIPM_USER" 2>/dev/null)"
fi
if getent passwd "$EXECUTOR_USER" >/dev/null 2>&1; then
    echo "  $EXECUTOR_USER: UID $(id -u "$EXECUTOR_USER"), shell $(user_shell "$EXECUTOR_USER"), primary group $(user_primary_group "$EXECUTOR_USER")"
    echo "    groups: $(id -Gn "$EXECUTOR_USER" 2>/dev/null)"
fi
echo "Runtime access: $AIPM_USER + $EXECUTOR_USER read-only via $RUNTIME_GROUP"
[ -f "$SUDOERS_FILE" ] && echo "Sudoers rule: $(cat "$SUDOERS_FILE")"

cat <<'NEXT_STEPS'

=== IDENTITY SETUP COMPLETE ===
This script performed NO database migration and NO service restart.

OPERATOR CHECKPOINTS (each is a separate, explicit step):
  CHECKPOINT 2 (filesystem):  verify permissions above; then STOP
  CHECKPOINT 3 (DB migration): sudo bash ops/migrate-aipm-state.sh   (separate script; STOP)
  CHECKPOINT 5 (systemd):      copy ops/systemd/aipm-*.service to /etc/systemd/system/,
                               systemctl daemon-reload, then restart services EXPLICITLY:
                               sudo systemctl restart aipm-telemetry aipm-events aipm-dashboard
NEXT_STEPS

exit 0
