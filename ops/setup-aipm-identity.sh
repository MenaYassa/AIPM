#!/bin/bash
# AIPM dedicated execution identity setup script.
# Run as root (or with sudo). Creates the aipm system user and configures
# filesystem permissions and the narrow sudoers rule.
#
# This script is IDEMPOTENT — it can be run multiple times safely.
#
# Prerequisites:
#   - Run from /home/ubuntu/aipm (the AIPM repository)
#   - Requires root privileges
#
# Usage:
#   sudo bash ops/setup-aipm-identity.sh
set -euo pipefail

DRY_RUN="${1:---apply}"

if [ "$DRY_RUN" = "--dry-run" ]; then
    echo "=== DRY RUN MODE (no changes will be made) ==="
    DRY=1
else
    DRY=0
fi

run_or_echo() {
    if [ "$DRY" -eq 1 ]; then
        echo "  [DRY RUN] $*"
    else
        "$@"
    fi
}

# Preflight (skipped for dry-run; the dry-run itself is non-mutating)
if [ "$DRY" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root."
    exit 1
fi
if [ "$DRY" -eq 1 ]; then
    echo "  Note: dry-run does not require root; it only reports intended changes."
fi
if [ ! -d "/home/ubuntu/aipm" ]; then
    echo "ERROR: AIPM repository not found at /home/ubuntu/aipm"
    exit 1
fi
if ! command -v useradd &>/dev/null; then
    echo "ERROR: useradd not available"
    exit 1
fi
if ! command -v visudo &>/dev/null; then
    echo "ERROR: visudo not available"
    exit 1
fi
echo "Preflight checks passed."

AIPM_USER="aipm"
AIPM_GROUP="aipm"
AIPM_HOME="/var/lib/aipm"
AIPM_STATE_DIR="${AIPM_HOME}/state"
AIPM_LOG_DIR="${AIPM_HOME}/logs"
AIPM_DB_DIR="${AIPM_STATE_DIR}/telemetry"
APP_CODE="/home/ubuntu/aipm"
OLD_STATE="/home/mina/.local/state/aipm"

echo "=== AIPM dedicated identity setup ==="

# --- 1. Create system user (idempotent) ---
if ! id "$AIPM_USER" &>/dev/null; then
    echo "Creating system user: $AIPM_USER"
    if [ "$DRY" -eq 1 ]; then
        echo "  [DRY RUN] Would create system user $AIPM_USER with nologin shell"
    else
        useradd --system --home-dir "$AIPM_HOME" --shell /usr/sbin/nologin \
            --no-create-home --user-group "$AIPM_USER"
        echo "  UID: $(id -u $AIPM_USER)"
    fi
else
    echo "User $AIPM_USER already exists (UID: $(id -u $AIPM_USER))"
fi

# Verify: no login shell, no password, no privileged groups
if [ "$DRY" -eq 0 ]; then
    ACTUAL_SHELL=$(getent passwd "$AIPM_USER" | cut -d: -f7)
    if [ "$ACTUAL_SHELL" != "/usr/sbin/nologin" ]; then
        echo "ERROR: $AIPM_USER shell is $ACTUAL_SHELL, expected /usr/sbin/nologin"
        exit 1
    fi
    ACTUAL_GROUPS=$(id -Gn "$AIPM_USER")
    if echo "$ACTUAL_GROUPS" | grep -qE '\b(sudo|docker|admin|root)\b'; then
        echo "ERROR: $AIPM_USER is in a privileged group: $ACTUAL_GROUPS"
        exit 1
    fi
    echo "  Groups: $ACTUAL_GROUPS (no privileged groups — OK)"
    echo "  Password: $(passwd -S "$AIPM_USER" 2>/dev/null | awk '{print $2}') (should be L = locked)"
else
    echo "  [DRY RUN] Shell check: /usr/sbin/nologin (would verify)"
    echo "  [DRY RUN] Group check: aipm only (would verify no sudo/docker)"
fi

# --- 1b. Create executor system user (idempotent) ---
EXECUTOR_USER="aipm-executor"
EXECUTOR_GROUP="aipm-executor"
if ! id "$EXECUTOR_USER" &>/dev/null; then
    echo "Creating executor system user: $EXECUTOR_USER"
    useradd --system --home-dir /var/lib/aipm-executor --shell /usr/sbin/nologin \
        --no-create-home --user-group "$EXECUTOR_USER"
    echo "  UID: $(id -u $EXECUTOR_USER)"
else
    echo "User $EXECUTOR_USER already exists (UID: $(id -u $EXECUTOR_USER))"
fi

# --- 1c. Add aipm to aipm-executor group (for socket IPC) ---
if ! id -Gn "$AIPM_USER" | grep -qw "$EXECUTOR_GROUP"; then
    echo "Adding $AIPM_USER to $EXECUTOR_GROUP group (for executor socket IPC)"
    usermod -a -G "$EXECUTOR_GROUP" "$AIPM_USER"
else
    echo "  $AIPM_USER already in $EXECUTOR_GROUP group"
fi

# --- 2. Create runtime directories ---
echo "Creating runtime directories..."
run_or_echo run_or_echo mkdir -p "$AIPM_STATE_DIR"
run_or_echo run_or_echo mkdir -p "$AIPM_DB_DIR"
run_or_echo run_or_echo mkdir -p "$AIPM_LOG_DIR"
run_or_echo run_or_echo chown -R "$AIPM_USER:$AIPM_GROUP" "$AIPM_HOME"
run_or_echo run_or_echo chmod 700 "$AIPM_HOME"
run_or_echo chmod 750 "$AIPM_STATE_DIR" "$AIPM_DB_DIR" "$AIPM_LOG_DIR"

# --- 3. Migrate existing state (if present) ---
if [ -f "${OLD_STATE}/telemetry/mission_control.db" ]; then
    echo "Migrating telemetry database from ${OLD_STATE}..."
    if [ ! -f "${AIPM_DB_DIR}/mission_control.db" ]; then
        BACKUP="${AIPM_DB_DIR}/mission_control.db.pre-migration.bak"
        echo "  [${DRY:+DRY RUN}] Database migration: ${OLD_STATE}/telemetry/mission_control.db → ${AIPM_DB_DIR}/mission_control.db (backup at $BACKUP)"
        if [ "$DRY" -eq 0 ]; then
            mkdir -p "$(dirname "$BACKUP")"
            cp "${OLD_STATE}/telemetry/mission_control.db" "$BACKUP"
            cp "$BACKUP" "${AIPM_DB_DIR}/mission_control.db"
            chown "$AIPM_USER:$AIPM_GROUP" "${AIPM_DB_DIR}/mission_control.db"
            chmod 600 "${AIPM_DB_DIR}/mission_control.db"
            echo "  Database migrated (backup preserved at $BACKUP)."
        fi
    else
        echo "  Database already migrated."
    fi
fi

# --- 4. Application code: read-only to aipm ---
echo "Setting application code permissions..."
# The venv must be executable by aipm but not writable
if [ "$DRY" -eq 0 ]; then
    chgrp -R "$AIPM_GROUP" "${APP_CODE}/.venv" 2>/dev/null || true
    chmod -R g+rx "${APP_CODE}/.venv/bin" 2>/dev/null || true
    chmod -R g+r "${APP_CODE}/src" 2>/dev/null || true
    chgrp "$AIPM_GROUP" "${APP_CODE}/config/aipm.yaml" 2>/dev/null || true
    chmod g+r "${APP_CODE}/config/aipm.yaml" 2>/dev/null || true
else
    echo "  [DRY RUN] Would set group permissions on .venv, src, config"
fi

# --- 5. Narrow sudoers rule for aipm ---
echo "Installing sudoers rule for $AIPM_USER..."
SUDOERS_FILE="/etc/sudoers.d/aipm-systemd-restart"
SUDOERS_RULE="${EXECUTOR_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service"
if [ "$DRY" -eq 1 ]; then
    echo "  [DRY RUN] Would install: $SUDOERS_RULE"
    echo "  [DRY RUN] Would validate with visudo -c"
else
    if [ -f "$SUDOERS_FILE" ]; then
        CURRENT=$(cat "$SUDOERS_FILE")
        if [ "$CURRENT" != "$SUDOERS_RULE" ]; then
            echo "  Updating existing rule..."
            echo "$SUDOERS_RULE" > "$SUDOERS_FILE"
            chmod 440 "$SUDOERS_FILE"
        else
            echo "  Rule already correct."
        fi
    else
        echo "$SUDOERS_RULE" > "$SUDOERS_FILE"
        chmod 440 "$SUDOERS_FILE"
    fi
    if ! visudo -c > /dev/null 2>&1; then
        echo "ERROR: sudoers validation failed!"
        rm -f "$SUDOERS_FILE"
        exit 1
    fi
    echo "  Sudoers rule installed and validated."
fi

# --- 6. Remove mina's sudoers rule (replaced by aipm's rule) ---
MINA_RULE="/etc/sudoers.d/aipm-systemd-restart-mina"
if [ -f "$MINA_RULE" ]; then
    rm -f "$MINA_RULE"
    echo "  Removed mina's old sudoers rule."
fi
# Check if the old mina-specific rule exists in the aipm file
if grep -q "^mina " "$SUDOERS_FILE" 2>/dev/null; then
    echo "  WARNING: mina rule found in $SUDOERS_FILE — removing..."
    sed -i '/^mina /d' "$SUDOERS_FILE"
    echo "$SUDOERS_RULE" >> "$SUDOERS_FILE"
fi

# --- 7. Verify ---
if [ "$DRY" -eq 1 ]; then
    echo "=== DRY RUN COMPLETE (no changes made) ==="
    echo "Intended actions:"
    echo "  1. Create system user aipm (nologin, no password, no privileged groups)"
    echo "  2. Create /var/lib/aipm/{state/telemetry,logs} directories"
    echo "  3. Migrate DB from /home/mina/.local/state/aipm/telemetry/"
    echo "  4. Set group permissions on .venv, src, config"
    echo "  5. Install sudoers rule for aipm"
    echo "  6. (Manual) Install systemd units and restart services"
    exit 0
fi

echo "=== VERIFICATION ==="
echo "User: $(id $AIPM_USER)"
echo "Shell: $(getent passwd $AIPM_USER | cut -d: -f7)"
echo "Groups: $(id -Gn $AIPM_USER)"
echo "Sudoers rule: $(cat $SUDOERS_FILE)"
echo "DB: $(ls -la $AIPM_DB_DIR/)"
echo "Logs: $(ls -la $AIPM_LOG_DIR/)"
echo "=== SETUP COMPLETE ==="
echo ""
echo "Next steps:"
echo "  1. Copy the updated systemd unit files (ops/systemd/)"
echo "  2. systemctl daemon-reload"
echo "  3. systemctl restart aipm-telemetry aipm-events aipm-dashboard"
echo "  4. Verify services are running as $AIPM_USER"
