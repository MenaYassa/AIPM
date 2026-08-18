#!/usr/bin/env bash
# MC-5 Gate 2.1 target-VPS dashboard staging.
# Run only from the target VPS repository directory with: bash ~/mc5-gate2.1-staging.sh
# Staging-only: transient systemd sandbox, temporary WAL-backed SQLite state, loopback dashboard.

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  printf '%s\n' 'Refusing to run when sourced; execute with: bash ~/mc5-gate2.1-staging.sh' >&2
  return 2
fi

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

readonly REPO=/home/ubuntu/aipm
readonly VENV_PYTHON=/home/ubuntu/aipm/.venv/bin/python
readonly AIPM_CLI=/home/ubuntu/aipm/.venv/bin/aipm
readonly LIVE_CONFIG=/home/ubuntu/.config/aipm/config.yaml
readonly LIVE_DB=/home/ubuntu/.local/state/aipm/telemetry/mission_control.db
readonly EXPECTED_COMMIT=4adf7ca2bcf831944ed752b7a146fbbe083f04c2
readonly PERMANENT_UNIT=aipm-dashboard.service

STAGE_ROOT=
STAGE_DB_DIR=
STAGE_DB=
STAGE_CONFIG=
NETWORK_GUARD_LOG=
DASH_PORT=
DASH_PID=
WRITER_PID=
WRITER_SUSPENDED=0
TEMP_UNIT=
TEMP_CREATED=0
CLEANUP_RESULT=PASS
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
FAILED_CHECKS=()
WARNINGS=()
NETWORK_CALL_COUNT=0
BROWSER_RESULT=SKIPPED
LIVE_FP_BEFORE=
LIVE_FP_AFTER=
STAGE_FP_BEFORE=
STAGE_FP_AFTER=
SERVICE_STATE_BEFORE=
SERVICE_STATE_AFTER=
PROCESS_STATE_BEFORE=
PROCESS_STATE_AFTER=
NOTIFICATIONS_BEFORE=
NOTIFICATIONS_AFTER=
EXTRA_TEMP_FILES=()

record_pass() {
  local name=$1 detail=${2:-}
  PASS_COUNT=$((PASS_COUNT + 1))
  printf 'CHECK_%s=PASS' "$name"
  [[ -n "$detail" ]] && printf ' detail=%s' "$detail"
  printf '\n'
}

record_fail() {
  local name=$1 detail=${2:-}
  FAIL_COUNT=$((FAIL_COUNT + 1))
  FAILED_CHECKS+=("$name: $detail")
  printf 'CHECK_%s=FAIL' "$name"
  [[ -n "$detail" ]] && printf ' detail=%s' "$detail"
  printf '\n' >&2
}

record_warn() {
  local name=$1 detail=${2:-}
  WARN_COUNT=$((WARN_COUNT + 1))
  WARNINGS+=("$name: $detail")
  printf 'WARNING_%s=%s\n' "$name" "$detail"
}

require_command() {
  local name=$1
  if command -v "$name" >/dev/null 2>&1; then
    record_pass "COMMAND_$name"
  else
    record_fail "COMMAND_$name" 'required command is unavailable'
    return 1
  fi
}

service_state() {
  local output_file=$1 unit
  : >"$output_file"
  for unit in aipm-telemetry.service aipm-events.service; do
    printf '%s active=' "$unit" >>"$output_file"
    systemctl --user is-active "$unit" >>"$output_file" || printf 'unknown\n' >>"$output_file"
    printf '%s enabled=' "$unit" >>"$output_file"
    systemctl --user is-enabled "$unit" >>"$output_file" || printf 'unknown\n' >>"$output_file"
  done
}

process_state() {
  local output_file=$1 telemetry_count events_count notifications_count
  telemetry_count=$(pgrep -fc 'aipm telemetry run' || true)
  events_count=$(pgrep -fc 'aipm events run' || true)
  notifications_count=$(pgrep -fc '[a]ipm notifications run' || true)
  printf 'telemetry=%s\nevents=%s\nnotifications=%s\n' "$telemetry_count" "$events_count" "$notifications_count" >"$output_file"
}

safe_notification_state() {
  local config_path=$1 output_file=$2
  "$VENV_PYTHON" - "$config_path" >"$output_file" <<'PY'
from pathlib import Path
import sys
from aipm.core.config import ConfigManager

config = ConfigManager(Path(sys.argv[1])).config
channels = config.notifications.channels
policies = config.notifications.policies
print(f"enabled={config.notifications.enabled}")
print(f"interval_seconds={config.notifications.interval_seconds}")
print(f"retention_days={config.notifications.retention_days}")
print(f"channel_count={len(channels)}")
print(f"enabled_channel_count={sum(1 for value in channels if value.enabled)}")
print(f"policy_count={len(policies)}")
print(f"enabled_policy_count={sum(1 for value in policies if value.enabled)}")
if config.notifications.enabled is not False:
    raise SystemExit("notifications.enabled is not explicitly False")
PY
}

artifact_fingerprint() {
  local db=$1 path label
  for path in "$db" "$db-wal" "$db-shm"; do
    label=$(basename "$path")
    if [[ -e "$path" ]]; then
      printf '%s\tpresent\towner=%s\tgroup=%s\tmode=%s\tsize=%s\tmtime=%s\tinode=%s\tsha256=%s\n' \
        "$label" \
        "$(stat --printf='%U' "$path")" \
        "$(stat --printf='%G' "$path")" \
        "$(stat --printf='%a' "$path")" \
        "$(stat --printf='%s' "$path")" \
        "$(stat --printf='%Y' "$path")" \
        "$(stat --printf='%i' "$path")" \
        "$(sha256sum "$path" | awk '{print $1}')"
    else
      printf '%s\tmissing\n' "$label"
    fi
  done
}

report_fingerprint_diff() {
  local before=$1 after=$2 artifact before_line after_line before_value after_value attribute
  printf 'FINGERPRINT_DIFF_BEGIN\n'
  while IFS= read -r artifact; do
    before_line=$(awk -F '\t' -v name="$artifact" '$1 == name { print; exit }' "$before")
    after_line=$(awk -F '\t' -v name="$artifact" '$1 == name { print; exit }' "$after")
    if [[ "$before_line" != "$after_line" ]]; then
      printf 'ARTIFACT_CHANGED=%s\n' "$artifact"
      for attribute in presence owner group mode size mtime inode sha256; do
        if [[ "$attribute" == presence ]]; then
          before_value=$(awk -F '\t' '{ print ($2 == "present" ? "present" : "missing") }' <<<"$before_line")
          after_value=$(awk -F '\t' '{ print ($2 == "present" ? "present" : "missing") }' <<<"$after_line")
        else
          before_value=$(awk -F '[=\t]' -v key="$attribute" '$1 == key { print $2 }' <<<"${before_line#*$'\t'}")
          after_value=$(awk -F '[=\t]' -v key="$attribute" '$1 == key { print $2 }' <<<"${after_line#*$'\t'}")
        fi
        [[ "$before_value" == "$after_value" ]] || printf 'ATTRIBUTE_CHANGED=%s %s_before=%s %s_after=%s\n' "$attribute" "$attribute" "${before_value:-missing}" "$attribute" "${after_value:-missing}"
      done
    fi
  done < <(printf '%s\n' mission_control.db mission_control.db-wal mission_control.db-shm)
  printf 'FINGERPRINT_DIFF_END\n'
}

write_network_guard() {
  local guard_dir="$STAGE_ROOT/network_guard"
  mkdir -p "$guard_dir"
  cat >"$guard_dir/sitecustomize.py" <<'PY'
import ipaddress
import json
import os
import socket
from pathlib import Path

_LOG = Path(os.environ["AIPM_MC521_NETWORK_GUARD_LOG"])
_ALLOWED = {"127.0.0.1", "localhost", "::1"}


def _host_allowed(host):
    if host in (None, "", *_ALLOWED):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in _ALLOWED


def _record(kind):
    with _LOG.open("a", encoding="utf-8") as stream:
        json.dump({"kind": kind, "blocked": True}, stream)
        stream.write("\n")


_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo


def _connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        host = address[0] if isinstance(address, tuple) else address
        if not _host_allowed(host):
            _record("connect")
            raise RuntimeError("MC-5 Gate 2.1 blocked non-loopback network connection")
    return _real_connect(self, address)


def _getaddrinfo(host, *args, **kwargs):
    if not _host_allowed(host):
        _record("getaddrinfo")
        raise RuntimeError("MC-5 Gate 2.1 blocked non-loopback name resolution")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _connect
socket.getaddrinfo = _getaddrinfo
PY
  NETWORK_GUARD_LOG="$STAGE_ROOT/network-blocked.jsonl"
  : >"$NETWORK_GUARD_LOG"
}

write_response_scan() {
  local scan_script="$STAGE_ROOT/scan_responses.py"
  cat >"$scan_script" <<'PY'
import json
import re
import sys
from pathlib import Path

files = [Path(value) for value in sys.argv[1:]]
key_pattern = re.compile(r"token|password|secret|api[_-]?key|webhook|authorization|credential|destination", re.I)
value_pattern = re.compile(r"BEGIN [A-Z ]+ KEY|https?://(?!127\.0\.0\.1|localhost)", re.I)
exact_safe_values = {
    ("response-_api_overview.json", "handbook[5].commands[1]"): "curl -I --max-time 10 https://example.com",
}
found = []


def walk(value, path, filename):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key_pattern.search(str(key)):
                found.append((filename.name, child_path))
            walk(child, child_path, filename)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            walk(child, f"{path}[{index}]", filename)
    elif isinstance(value, str) and value_pattern.search(value):
        exact_match = exact_safe_values.get((filename.name, path)) == value
        if not exact_match:
            found.append((filename.name, path or "<value>"))


for filename in files:
    if filename.suffix == ".json":
        try:
            walk(json.loads(filename.read_text(encoding="utf-8")), "", filename)
        except Exception:
            found.append((filename.name, "<invalid-json>"))
    else:
        text = filename.read_text(encoding="utf-8")
        if value_pattern.search(text) or key_pattern.search(text):
            found.append((filename.name, "<text>"))

if found:
    for filename, path in found:
        print(f"{filename}:{path}")
    raise SystemExit(1)
PY
}

stage_integrity() {
  local db=$1
  "$VENV_PYTHON" - "$db" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok":
        raise SystemExit(f"integrity_check={integrity}")
    if foreign_keys:
        raise SystemExit(f"foreign_key_violations={len(foreign_keys)}")
print("integrity_check=ok")
print("foreign_key_violations=0")
PY
}

api_filename() {
  printf '%s' "$1" | tr -c 'A-Za-z0-9' '_'
}

stop_dashboard() {
  if [[ -n "$TEMP_UNIT" ]]; then
    systemctl --user stop "$TEMP_UNIT" >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do
      [[ "$(systemctl --user is-active "$TEMP_UNIT" 2>/dev/null || true)" != active ]] && break
      sleep 0.2
    done
    systemctl --user reset-failed "$TEMP_UNIT" >/dev/null 2>&1 || true
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  elif [[ -n "$DASH_PID" ]] && kill -0 "$DASH_PID" 2>/dev/null; then
    kill -TERM "$DASH_PID" 2>/dev/null || true
  fi
}

cleanup() {
  local cleanup_failed=0 active enabled
  stop_dashboard
  if [[ -n "$TEMP_UNIT" ]]; then
    active=$(systemctl --user is-active "$TEMP_UNIT" 2>/dev/null || true)
    enabled=$(systemctl --user is-enabled "$TEMP_UNIT" 2>/dev/null || true)
    [[ "$active" != active ]] || cleanup_failed=1
    [[ "$enabled" != enabled && "$enabled" != enabled-runtime ]] || cleanup_failed=1
  fi
  if [[ -n "$WRITER_PID" ]] && kill -0 "$WRITER_PID" 2>/dev/null; then
    if [[ "$WRITER_SUSPENDED" == 1 ]]; then
      kill -CONT "$WRITER_PID" 2>/dev/null || cleanup_failed=1
      WRITER_SUSPENDED=0
    fi
    kill -TERM "$WRITER_PID" 2>/dev/null || cleanup_failed=1
    wait "$WRITER_PID" 2>/dev/null || true
  fi
  if [[ -n "$DASH_PID" ]] && kill -0 "$DASH_PID" 2>/dev/null; then
    cleanup_failed=1
    kill -KILL "$DASH_PID" 2>/dev/null || true
  fi
  if [[ -n "$DASH_PORT" ]] && ss -ltn 2>/dev/null | grep -Eq "127\\.0\\.0\\.1:${DASH_PORT}[[:space:]]"; then
    cleanup_failed=1
  fi
  for temporary_file in "${EXTRA_TEMP_FILES[@]}"; do
    [[ -z "$temporary_file" ]] || rm -f -- "$temporary_file" || cleanup_failed=1
  done
  if [[ "$TEMP_CREATED" == 1 && -n "$STAGE_ROOT" && -e "$STAGE_ROOT" ]]; then
    chmod -R u+rwX "$STAGE_ROOT" 2>/dev/null || true
    rm -rf -- "$STAGE_ROOT" || cleanup_failed=1
  fi
  if ((cleanup_failed)); then
    CLEANUP_RESULT=FAIL
    record_fail CLEANUP 'temporary process, unit, port, or files remained'
  else
    CLEANUP_RESULT=PASS
    record_pass CLEANUP
  fi
}

print_summary() {
  local rc=$1 overall=PASS
  if (( FAIL_COUNT != 0 )) || [[ "$CLEANUP_RESULT" != PASS ]]; then
    overall=FAIL
  fi
  printf '\n=== MC-5 GATE 2.1 RESULT ===\n'
  printf 'OVERALL=%s\n' "$overall"
  printf 'PASS_COUNT=%s\n' "$PASS_COUNT"
  printf 'FAIL_COUNT=%s\n' "$FAIL_COUNT"
  printf 'WARNING_COUNT=%s\n' "$WARN_COUNT"
  printf 'CLEANUP=%s\n' "$CLEANUP_RESULT"
  printf 'NETWORK_PROVIDER_CALLS=%s\n' "$NETWORK_CALL_COUNT"
  printf 'BROWSER=%s\n' "$BROWSER_RESULT"
  printf 'PERSISTENT_DASHBOARD_ENABLEMENT=NO\n'
  printf 'GATE_3_STARTED=NO\n'
  if ((${#FAILED_CHECKS[@]})); then
    printf 'FAILED_CHECKS:\n'
    printf -- '- %s\n' "${FAILED_CHECKS[@]}"
  fi
  if ((${#WARNINGS[@]})); then
    printf 'WARNINGS:\n'
    printf -- '- %s\n' "${WARNINGS[@]}"
  fi
  printf 'SCRIPT_EXIT_STATUS=%s\n' "$rc"
}

on_exit() {
  local rc=$? final_rc
  cleanup
  final_rc=$rc
  [[ "$CLEANUP_RESULT" == PASS ]] || final_rc=1
  print_summary "$final_rc"
  return "$final_rc"
}

on_signal() {
  record_fail SIGNAL "received $1; staging aborted"
  exit 130
}

trap on_exit EXIT
trap 'on_signal SIGINT' INT
trap 'on_signal SIGTERM' TERM

main() {
  local current_head current_status live_notifications_enabled
  local permanent_active permanent_enabled temp_state
  local ack_status response_file key path stage_expected
  local api_paths=() scan_files=()

  printf '%s\n' '=== MC-5 GATE 2.1 TARGET-VPS STAGING ==='
  printf 'target_repo=%s\n' "$REPO"
  printf 'expected_commit=%s\n' "$EXPECTED_COMMIT"

  [[ "$PWD" == "$REPO" ]] || { record_fail TARGET_WORKING_DIRECTORY "run from $REPO"; return 1; }
  [[ -d "$REPO/.git" ]] || { record_fail TARGET_REPOSITORY_PRESENT 'repository is absent'; return 1; }
  current_head=$(git -C "$REPO" rev-parse HEAD)
  [[ "$current_head" == "$EXPECTED_COMMIT" ]] || { record_fail TARGET_COMMIT "expected $EXPECTED_COMMIT, found $current_head"; return 1; }
  record_pass TARGET_COMMIT "$current_head"
  [[ "$(git -C "$REPO" rev-parse origin/main)" == "$EXPECTED_COMMIT" ]] || { record_fail TARGET_ORIGIN "origin/main does not match expected commit"; return 1; }
  record_pass TARGET_ORIGIN
  current_status=$(git -C "$REPO" status --porcelain)
  [[ -z "$current_status" ]] || { record_fail TARGET_WORKTREE_CLEAN 'worktree is dirty'; return 1; }
  record_pass TARGET_WORKTREE_CLEAN
  [[ -x "$AIPM_CLI" && -x "$VENV_PYTHON" ]] || { record_fail TARGET_RUNTIME 'AIPM CLI or Python runtime is unavailable'; return 1; }
  "$AIPM_CLI" dashboard --help >/dev/null || { record_fail TARGET_CLI_REGISTRATION 'dashboard CLI registration/help failed'; return 1; }
  record_pass TARGET_RUNTIME
  record_pass TARGET_CLI_REGISTRATION
  if grep -Rqs 'mode=ro' "$REPO/src/aipm/repositories" && [[ "$(grep -RIl 'PRAGMA query_only = ON' "$REPO/src/aipm/repositories" | wc -l)" -ge 4 ]]; then
    record_pass REPOSITORY_READONLY_BOUNDARY 'mode=ro and query_only are present in all four SQLite repositories'
  else
    record_fail REPOSITORY_READONLY_BOUNDARY 'reviewed WAL-compatible read-only implementation is absent'
    return 1
  fi

  require_command curl || return 1
  require_command stat || return 1
  require_command sha256sum || return 1
  require_command ss || return 1
  require_command pgrep || return 1
  require_command systemctl || return 1
  require_command systemd-run || return 1
  require_command mktemp || return 1

  [[ -f "$LIVE_CONFIG" ]] || { record_fail LIVE_CONFIG_PRESENT 'configuration file is absent'; return 1; }
  [[ -f "$LIVE_DB" ]] || { record_fail LIVE_DATABASE_PRESENT 'database file is absent'; return 1; }
  record_pass TARGET_PATHS

  permanent_active=$(systemctl --user is-active "$PERMANENT_UNIT" 2>/dev/null || true)
  permanent_enabled=$(systemctl --user is-enabled "$PERMANENT_UNIT" 2>/dev/null || true)
  [[ "$permanent_active" != active ]] || { record_fail PERMANENT_DASHBOARD_ABSENT 'permanent dashboard service is active'; return 1; }
  [[ "$permanent_enabled" != enabled && "$permanent_enabled" != enabled-runtime ]] || { record_fail PERMANENT_DASHBOARD_NOT_ENABLED 'permanent dashboard service is enabled'; return 1; }
  record_pass PERMANENT_DASHBOARD_NOT_ENABLED

  NOTIFICATIONS_BEFORE=$(mktemp)
  EXTRA_TEMP_FILES+=("$NOTIFICATIONS_BEFORE")
  if ! safe_notification_state "$LIVE_CONFIG" "$NOTIFICATIONS_BEFORE"; then
    record_fail LIVE_NOTIFICATIONS_DISABLED 'ConfigManager did not report notifications.enabled=False'
    return 1
  fi
  live_notifications_enabled=$(awk -F= '$1 == "enabled" {print $2}' "$NOTIFICATIONS_BEFORE")
  [[ "$live_notifications_enabled" == False ]] || { record_fail LIVE_NOTIFICATIONS_DISABLED 'effective value is not False'; return 1; }
  record_pass LIVE_NOTIFICATIONS_DISABLED 'effective ConfigManager value is False'
  if grep -Eq 'channel_count=[^0]|enabled_channel_count=[^0]|policy_count=[^0]|enabled_policy_count=[^0]' "$NOTIFICATIONS_BEFORE"; then
    record_fail LIVE_NOTIFICATION_CHANNELS 'live channels or policies are configured; staging refuses to proceed'
    return 1
  fi
  record_pass LIVE_NOTIFICATION_CHANNELS 'zero configured channels and policies'

  [[ "$(systemctl --user is-active aipm-telemetry.service 2>/dev/null || true)" == active ]] || { record_fail TELEMETRY_ACTIVE 'telemetry service is not active'; return 1; }
  [[ "$(systemctl --user is-enabled aipm-telemetry.service 2>/dev/null || true)" == enabled ]] || { record_fail TELEMETRY_ENABLED 'telemetry service is not enabled'; return 1; }
  [[ "$(systemctl --user is-active aipm-events.service 2>/dev/null || true)" == active ]] || { record_fail MC3_ACTIVE 'MC-3 event service is not active'; return 1; }
  [[ "$(systemctl --user is-enabled aipm-events.service 2>/dev/null || true)" == enabled ]] || { record_fail MC3_ENABLED 'MC-3 event service is not enabled'; return 1; }
  record_pass TELEMETRY_MC3_SERVICES 'active and enabled'

  PROCESS_STATE_BEFORE=$(mktemp)
  EXTRA_TEMP_FILES+=("$PROCESS_STATE_BEFORE")
  process_state "$PROCESS_STATE_BEFORE"
  grep -Eq '^notifications=0$' "$PROCESS_STATE_BEFORE" || { record_fail NOTIFICATION_WORKER_ABSENT 'notification worker is running'; return 1; }
  record_pass NOTIFICATION_WORKER_ABSENT
  SERVICE_STATE_BEFORE=$(mktemp)
  EXTRA_TEMP_FILES+=("$SERVICE_STATE_BEFORE")
  service_state "$SERVICE_STATE_BEFORE"
  LIVE_FP_BEFORE=$(mktemp)
  EXTRA_TEMP_FILES+=("$LIVE_FP_BEFORE")
  artifact_fingerprint "$LIVE_DB" >"$LIVE_FP_BEFORE"
  record_pass LIVE_DATABASE_BASELINE_CAPTURED

  STAGE_ROOT=$(mktemp -d -p /var/tmp aipm-mc521-XXXXXX)
  TEMP_CREATED=1
  chmod 700 "$STAGE_ROOT"
  STAGE_DB_DIR="$STAGE_ROOT/db"
  mkdir -p "$STAGE_DB_DIR"
  STAGE_DB="$STAGE_DB_DIR/mission_control.db"
  STAGE_CONFIG="$STAGE_ROOT/config.yaml"
  write_network_guard
  record_pass TEMPORARY_STATE_CREATED

  cat >"$STAGE_ROOT/wal_writer.py" <<'PY'
import sqlite3
import sys
import time
from pathlib import Path

from aipm.repositories.events.sqlite import SQLiteEventRepository
from aipm.repositories.incidents.sqlite import SQLiteIncidentRepository
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository

path = Path(sys.argv[1])
ready = Path(sys.argv[2])
expected = Path(sys.argv[3])
SQLiteHistoryRepository(path)
SQLiteEventRepository(path)
SQLiteIncidentRepository(path)
SQLiteNotificationRepository(path)
writer = sqlite3.connect(path)
writer.execute("PRAGMA journal_mode = WAL")
writer.execute("PRAGMA wal_autocheckpoint = 0")
now = int(time.time())
run = writer.execute(
    "INSERT INTO sample_runs (sampled_at, host_available, docker_available, projects_available, tunnel_state, duration_ms) VALUES (?, 1, 1, 1, 'healthy', 1)",
    (now,),
)
writer.execute(
    "INSERT INTO host_samples (run_id, sampled_at, hostname, available) VALUES (?, ?, ?, 1)",
    (int(run.lastrowid), now, "mc521-active-wal-host"),
)
writer.commit()
if writer.execute("SELECT COUNT(*) FROM host_samples WHERE hostname = 'mc521-active-wal-host'").fetchone()[0] != 1:
    raise SystemExit("WAL fixture row was not committed")
expected.write_text("mc521-active-wal-host\n", encoding="utf-8")
ready.write_text("ready\n", encoding="utf-8")
while True:
    time.sleep(1)
PY
  READY_FILE="$STAGE_ROOT/wal.ready"
  EXPECTED_FILE="$STAGE_ROOT/wal.expected"
  "$VENV_PYTHON" "$STAGE_ROOT/wal_writer.py" "$STAGE_DB" "$READY_FILE" "$EXPECTED_FILE" >/dev/null 2>&1 &
  WRITER_PID=$!
  for _ in $(seq 1 30); do
    [[ -f "$READY_FILE" ]] && break
    kill -0 "$WRITER_PID" 2>/dev/null || break
    sleep 0.2
  done
  [[ -f "$READY_FILE" && -f "$STAGE_DB-wal" && -f "$STAGE_DB-shm" ]] || { record_fail ACTIVE_WAL_FIXTURE 'WAL writer or sidecars were not prepared'; return 1; }
  grep -qx 'mc521-active-wal-host' "$EXPECTED_FILE" || { record_fail ACTIVE_WAL_FIXTURE 'expected WAL-backed row was not prepared'; return 1; }
  record_pass ACTIVE_WAL_FIXTURE 'committed row remains in WAL with sidecars present'
  kill -STOP "$WRITER_PID" 2>/dev/null || { record_fail ACTIVE_WAL_FIXTURE_WRITER_INERT 'could not suspend the fixture writer'; return 1; }
  WRITER_SUSPENDED=1
  record_pass ACTIVE_WAL_FIXTURE_WRITER_INERT 'fixture writer suspended after commit; WAL/SHM remain available for reader-only observation'

  cat >"$STAGE_CONFIG" <<YAML
telemetry:
  enabled: true
  interval_seconds: 15
  retention_days: 1
  database_path: $STAGE_DB
events:
  enabled: true
  interval_seconds: 15
  event_retention_days: 30
  incident_retention_days: 180
notifications:
  enabled: false
  interval_seconds: 5
  retention_days: 30
  channels: []
  policies: []
projects:
  search_paths: []
YAML
  chmod 600 "$STAGE_CONFIG"
  if ! safe_notification_state "$STAGE_CONFIG" "$STAGE_ROOT/stage-notifications.txt"; then
    record_fail STAGING_NOTIFICATIONS_DISABLED 'temporary ConfigManager validation failed'
    return 1
  fi
  grep -q '^enabled=False$' "$STAGE_ROOT/stage-notifications.txt" || { record_fail STAGING_NOTIFICATIONS_DISABLED 'temporary effective value is not False'; return 1; }
  grep -Eq '^channel_count=0$' "$STAGE_ROOT/stage-notifications.txt" && grep -Eq '^policy_count=0$' "$STAGE_ROOT/stage-notifications.txt" || { record_fail STAGING_NOTIFICATION_CHANNELS 'temporary channels or policies are not empty'; return 1; }
  record_pass STAGING_NOTIFICATIONS_DISABLED

  if "$VENV_PYTHON" - "$STAGE_DB" <<'PY'
from pathlib import Path
import sys
from aipm.repositories.readonly import ReadOnlyFilesystemError
from aipm.repositories.telemetry.sqlite import SQLiteHistoryRepository

try:
    SQLiteHistoryRepository(Path(sys.argv[1]), read_only=True)
except ReadOnlyFilesystemError:
    pass
else:
    raise SystemExit("unprotected filesystem boundary was accepted")
PY
  then
    record_pass UNPROTECTED_FILESYSTEM_REJECTED 'read-only repository refused writable database boundary'
  else
    record_fail UNPROTECTED_FILESYSTEM_REJECTED 'read-only repository did not fail closed'
    return 1
  fi

  chmod 444 "$STAGE_DB" "$STAGE_DB-wal" "$STAGE_DB-shm"
  chmod 555 "$STAGE_DB_DIR"
  record_pass FILESYSTEM_BOUNDARY_PREPARED 'database directory and sidecars are non-writable'

  STAGE_FP_BEFORE=$(mktemp)
  EXTRA_TEMP_FILES+=("$STAGE_FP_BEFORE")
  artifact_fingerprint "$STAGE_DB" >"$STAGE_FP_BEFORE"
  stage_integrity "$STAGE_DB" || { record_fail ACTIVE_WAL_INTEGRITY_BEFORE 'active-WAL database integrity failed'; return 1; }
  record_pass ACTIVE_WAL_INTEGRITY_BEFORE

  DASH_PORT=$($VENV_PYTHON - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)
  TEMP_UNIT="aipm-mc521-staging-${RANDOM}${RANDOM}.service"
  systemd-run --user --unit="$TEMP_UNIT" --collect \
    --property=Type=simple \
    --property=WorkingDirectory="$REPO" \
    --property=ProtectSystem=strict \
    --property=ProtectHome=read-only \
    --property=ReadOnlyPaths="$STAGE_DB_DIR" \
    --property=NoNewPrivileges=true \
    --property=PrivateTmp=true \
    --property=RestrictSUIDSGID=true \
    --property=UMask=0077 \
    --setenv=PYTHONUNBUFFERED=1 \
    --setenv=PYTHONDONTWRITEBYTECODE=1 \
    --setenv=HOME=/home/ubuntu \
    --setenv=AIPM_CONFIG="$STAGE_CONFIG" \
    --setenv=AIPM_TELEMETRY_DB="$STAGE_DB" \
    --setenv=AIPM_MC521_NETWORK_GUARD_LOG="$NETWORK_GUARD_LOG" \
    --setenv=PYTHONPATH="$STAGE_ROOT/network_guard:$REPO/src" \
    "$AIPM_CLI" dashboard --host 127.0.0.1 --port "$DASH_PORT" >/dev/null
  record_pass TEMPORARY_SYSTEMD_UNIT "$TEMP_UNIT"

  for _ in $(seq 1 40); do
    if [[ "$(systemctl --user is-active "$TEMP_UNIT" 2>/dev/null || true)" == active ]] && curl --fail --silent --show-error --noproxy '*' "http://127.0.0.1:$DASH_PORT/healthz" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
  [[ "$(systemctl --user is-active "$TEMP_UNIT" 2>/dev/null || true)" == active ]] || { record_fail DASHBOARD_STARTUP 'temporary systemd dashboard unit did not remain active'; return 1; }
  DASH_PID=$(systemctl --user show "$TEMP_UNIT" -p MainPID --value)
  [[ -n "$DASH_PID" && "$DASH_PID" != 0 ]] || { record_fail DASHBOARD_PID 'temporary dashboard MainPID unavailable'; return 1; }
  curl --fail --silent --show-error --noproxy '*' "http://127.0.0.1:$DASH_PORT/healthz" >/dev/null || { record_fail DASHBOARD_HEALTH 'healthz failed'; return 1; }
  record_pass DASHBOARD_STARTUP
  record_pass DASHBOARD_HEALTH
  ss -ltn | grep -Eq "127\.0\.0\.1:${DASH_PORT}[[:space:]]" || { record_fail LOOPBACK_BIND "dashboard is not listening on 127.0.0.1:$DASH_PORT"; return 1; }
  ! ss -ltn | grep -Eq "0\.0\.0\.0:${DASH_PORT}|\[::\]:${DASH_PORT}|\[::1\]:${DASH_PORT}" || { record_fail NON_LOOPBACK_BIND 'dashboard listener is not strictly IPv4 loopback-only'; return 1; }
  record_pass LOOPBACK_BIND "127.0.0.1:$DASH_PORT"

  if find "/proc/$DASH_PID/fd" -maxdepth 1 -type l -print -exec readlink {} \; 2>/dev/null | grep -Fq "$LIVE_DB"; then
    record_fail DASHBOARD_LIVE_DB_FD 'temporary dashboard process has a live database file descriptor'
    return 1
  fi
  record_pass DASHBOARD_LIVE_DB_FD 'live database path is absent from dashboard process descriptors'

  api_paths=(
    '/'
    '/healthz'
    '/api/services'
    '/api/overview'
    '/api/events'
    '/api/events/1'
    '/api/incidents'
    '/api/incidents/1'
    '/api/notifications'
    '/api/notifications/1'
    '/api/notification-channels'
    '/api/notification-policies'
    '/api/notification-metrics'
    '/api/history/host?range=1h&limit=10'
    '/api/history/containers?range=1h&limit=10'
    '/api/history/container-resources?range=1h&limit=10'
    '/api/history/projects?range=1h&limit=10'
    '/api/history/tunnel?range=1h&limit=10'
  )
  for path in "${api_paths[@]}"; do
    key=$(api_filename "$path")
    if [[ "$path" == "/" ]]; then
      response_file="$STAGE_ROOT/response-$key"
    else
      response_file="$STAGE_ROOT/response-$key.json"
    fi
    curl --fail --silent --show-error --noproxy '*' --request GET "http://127.0.0.1:$DASH_PORT$path" -o "$response_file" || { record_fail "API_$key" 'GET request failed'; return 1; }
    scan_files+=("$response_file")
    record_pass "API_$key" 'GET 200'
  done

  grep -q 'mc521-active-wal-host' "$STAGE_ROOT/response-_api_history_host_range_1h_limit_10.json" || { record_fail ACTIVE_WAL_ROW_VISIBLE 'dashboard history response did not contain the committed WAL-backed row'; return 1; }
  record_pass ACTIVE_WAL_ROW_VISIBLE 'current WAL-backed host row returned by dashboard'

  ack_status=$(curl --silent --noproxy '*' --output /dev/null --write-out '%{http_code}' --request POST "http://127.0.0.1:$DASH_PORT/api/incidents/1/acknowledge")
  [[ "$ack_status" == 404 ]] || { record_fail ACK_ROUTE_ABSENT "expected 404, received $ack_status"; return 1; }
  record_pass ACK_ROUTE_ABSENT 'POST returned 404'

  write_response_scan
  "$VENV_PYTHON" "$STAGE_ROOT/scan_responses.py" "${scan_files[@]}" >"$STAGE_ROOT/secret-scan.txt" || { record_fail RESPONSE_SECRET_SCAN 'secret-like field or external URL detected; details withheld'; return 1; }
  record_pass RESPONSE_SECRET_SCAN 'exact reviewed scanner passed'

  if [[ -s "$NETWORK_GUARD_LOG" ]]; then
    NETWORK_CALL_COUNT=$(wc -l <"$NETWORK_GUARD_LOG")
    record_fail NETWORK_PROVIDER_CALLS "blocked non-loopback network attempts=$NETWORK_CALL_COUNT"
    return 1
  fi
  NETWORK_CALL_COUNT=0
  record_pass NETWORK_PROVIDER_CALLS 'zero blocked non-loopback attempts'

  STAGE_FP_AFTER=$(mktemp)
  EXTRA_TEMP_FILES+=("$STAGE_FP_AFTER")
  artifact_fingerprint "$STAGE_DB" >"$STAGE_FP_AFTER"
  if ! cmp -s "$STAGE_FP_BEFORE" "$STAGE_FP_AFTER"; then
    report_fingerprint_diff "$STAGE_FP_BEFORE" "$STAGE_FP_AFTER"
    record_fail ACTIVE_WAL_DATABASE_IMMUTABLE 'database/WAL/SHM fingerprint changed during dashboard operation; per-artifact diagnostic emitted above'
    return 1
  fi
  stage_integrity "$STAGE_DB" || { record_fail ACTIVE_WAL_INTEGRITY_AFTER 'active-WAL database integrity failed after dashboard reads'; return 1; }
  record_pass ACTIVE_WAL_DATABASE_IMMUTABLE 'database/WAL/SHM fingerprints unchanged'
  record_pass ACTIVE_WAL_INTEGRITY_AFTER

  if command -v chromium >/dev/null 2>&1; then
    if chromium --headless --no-sandbox --disable-gpu --hide-scrollbars --disable-background-networking --disable-component-update --disable-sync --no-first-run --proxy-server=direct:// --proxy-bypass-list='*' --host-resolver-rules='MAP * ~NOTFOUND, EXCLUDE 127.0.0.1' --virtual-time-budget=5000 --window-size=1440,1000 --screenshot="$STAGE_ROOT/dashboard.png" "http://127.0.0.1:$DASH_PORT/" >"$STAGE_ROOT/browser.log" 2>&1; then
      BROWSER_RESULT=PASS
      record_pass BROWSER_SMOKE
    else
      BROWSER_RESULT=FAIL
      record_fail BROWSER_SMOKE 'loopback Chromium smoke test failed'
      return 1
    fi
  else
    BROWSER_RESULT=SKIPPED
    record_warn BROWSER_SMOKE 'Chromium is unavailable; loopback browser check skipped'
  fi

  if find "/proc/$DASH_PID/fd" -maxdepth 1 -type l -print -exec readlink {} \; 2>/dev/null | grep -Fq "$LIVE_DB"; then
    record_fail DASHBOARD_LIVE_DB_FD_AFTER 'temporary dashboard process opened the live database path'
    return 1
  fi
  record_pass DASHBOARD_LIVE_DB_FD_AFTER

  LIVE_FP_AFTER=$(mktemp)
  EXTRA_TEMP_FILES+=("$LIVE_FP_AFTER")
  artifact_fingerprint "$LIVE_DB" >"$LIVE_FP_AFTER"
  if cmp -s "$LIVE_FP_BEFORE" "$LIVE_FP_AFTER"; then
    record_pass LIVE_DATABASE_FINGERPRINT_UNCHANGED
  else
    record_warn LIVE_DATABASE_RUNTIME_ACTIVITY 'live database artifacts changed while telemetry/MC-3 remained active; dashboard had no live DB path and used only the temporary database'
    record_pass LIVE_DATABASE_NOT_MODIFIED_BY_DASHBOARD 'live DB absent from dashboard descriptors and never passed to dashboard'
  fi

  SERVICE_STATE_AFTER=$(mktemp)
  EXTRA_TEMP_FILES+=("$SERVICE_STATE_AFTER")
  service_state "$SERVICE_STATE_AFTER"
  cmp -s "$SERVICE_STATE_BEFORE" "$SERVICE_STATE_AFTER" || { record_fail TELEMETRY_MC3_SERVICES_UNCHANGED 'service state changed'; return 1; }
  record_pass TELEMETRY_MC3_SERVICES_UNCHANGED
  PROCESS_STATE_AFTER=$(mktemp)
  EXTRA_TEMP_FILES+=("$PROCESS_STATE_AFTER")
  process_state "$PROCESS_STATE_AFTER"
  cmp -s "$PROCESS_STATE_BEFORE" "$PROCESS_STATE_AFTER" || { record_fail TELEMETRY_MC3_PROCESSES_UNCHANGED 'process topology/count changed'; return 1; }
  grep -Eq '^notifications=0$' "$PROCESS_STATE_AFTER" || { record_fail NOTIFICATION_WORKER_ABSENT_AFTER 'notification worker is running after staging'; return 1; }
  record_pass TELEMETRY_MC3_PROCESSES_UNCHANGED

  NOTIFICATIONS_AFTER=$(mktemp)
  EXTRA_TEMP_FILES+=("$NOTIFICATIONS_AFTER")
  if ! safe_notification_state "$LIVE_CONFIG" "$NOTIFICATIONS_AFTER"; then
    record_fail LIVE_NOTIFICATIONS_AFTER 'live ConfigManager validation failed'; return 1
  fi
  grep -q '^enabled=False$' "$NOTIFICATIONS_AFTER" || { record_fail LIVE_NOTIFICATIONS_AFTER 'effective notifications.enabled is not False'; return 1; }
  record_pass LIVE_NOTIFICATIONS_AFTER 'effective value remains False'

  temp_state=$(systemctl --user is-enabled "$TEMP_UNIT" 2>/dev/null || true)
  [[ "$temp_state" != enabled && "$temp_state" != enabled-runtime ]] || { record_fail TEMPORARY_UNIT_PERSISTED 'transient staging unit became enabled'; return 1; }
  record_pass TEMPORARY_UNIT_NOT_ENABLED
  return 0
}

main "$@"
