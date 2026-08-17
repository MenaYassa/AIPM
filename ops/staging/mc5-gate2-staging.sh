#!/usr/bin/env bash
# MC-5 Gate 2 target-VPS dashboard staging.
# Run only as: bash mc5-gate2-staging.sh
# Staging-only: temporary SQLite/configuration, loopback dashboard, no persistent service.

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  printf '%s\n' 'Refusing to run when sourced; execute with: bash mc5-gate2-staging.sh' >&2
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
readonly EXPECTED_COMMIT=f7004d8311b322ba19b382e372585bafa34ca47b

STAGE_ROOT=
STAGE_DB=
STAGE_CONFIG=
DASH_PID=
DASH_PORT=
NETWORK_GUARD_LOG=
TEMP_CREATED=0
CLEANUP_RESULT=PASS
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
FAILED_CHECKS=()
WARNINGS=()
LIVE_DB_HASH_BEFORE=
LIVE_DB_HASH_AFTER=
LIVE_DB_META_BEFORE=
LIVE_DB_META_AFTER=
STAGE_DB_HASH_BEFORE_READS=
STAGE_DB_HASH_AFTER_READS=
SERVICE_STATE_BEFORE=
SERVICE_STATE_AFTER=
PROCESS_STATE_BEFORE=
PROCESS_STATE_AFTER=
NOTIFICATIONS_BEFORE=
NOTIFICATIONS_AFTER=
BROWSER_RESULT=SKIPPED
NETWORK_CALL_COUNT=0
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
  local output_file=$1
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

safe_db_meta() {
  local db=$1
  stat --printf='owner=%U group=%G mode=%a size=%s mtime=%Y inode=%i\n' "$db"
}

hash_db() {
  sha256sum "$1" | awk '{print $1}'
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

_LOG = Path(os.environ["AIPM_MC5_NETWORK_GUARD_LOG"])
_ALLOWED = {"127.0.0.1", "localhost", "::1"}


def _record(kind, value):
    with _LOG.open("a", encoding="utf-8") as stream:
        json.dump({"kind": kind, "blocked": True}, stream)
        stream.write("\n")


def _host_allowed(host):
    if host in (None, "", *_ALLOWED):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in _ALLOWED


_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo


def _connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        host = address[0] if isinstance(address, tuple) else address
        if not _host_allowed(host):
            _record("connect", host)
            raise RuntimeError("MC-5 Gate 2 blocked non-loopback network connection")
    return _real_connect(self, address)


def _getaddrinfo(host, *args, **kwargs):
    if not _host_allowed(host):
        _record("getaddrinfo", host)
        raise RuntimeError("MC-5 Gate 2 blocked non-loopback name resolution")
    return _real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _connect
socket.getaddrinfo = _getaddrinfo
PY
  NETWORK_GUARD_LOG="$guard_dir/blocked.jsonl"
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

cleanup() {
  local cleanup_failed=0
  if [[ -n "$DASH_PID" ]] && kill -0 "$DASH_PID" 2>/dev/null; then
    kill -TERM "$DASH_PID" 2>/dev/null || cleanup_failed=1
    for _ in $(seq 1 20); do
      kill -0 "$DASH_PID" 2>/dev/null || break
      sleep 0.2
    done
    if kill -0 "$DASH_PID" 2>/dev/null; then
      kill -KILL "$DASH_PID" 2>/dev/null || cleanup_failed=1
      wait "$DASH_PID" 2>/dev/null || true
    fi
  fi
  if [[ -n "$DASH_PORT" ]] && ss -ltn 2>/dev/null | grep -Eq "127\\.0\\.0\\.1:${DASH_PORT}[[:space:]]"; then
    cleanup_failed=1
  fi
  for temporary_file in "${EXTRA_TEMP_FILES[@]}"; do
    [[ -z "$temporary_file" ]] || rm -f -- "$temporary_file" || cleanup_failed=1
  done
  if [[ "$TEMP_CREATED" == 1 && -n "$STAGE_ROOT" && -e "$STAGE_ROOT" ]]; then
    rm -rf -- "$STAGE_ROOT" || cleanup_failed=1
  fi
  if ((cleanup_failed)); then
    CLEANUP_RESULT=FAIL
    record_fail CLEANUP 'temporary process, port, or files remained'
  else
    CLEANUP_RESULT=PASS
  fi
}

print_summary() {
  local rc=$1 overall=PASS
  if (( FAIL_COUNT != 0 )) || [[ "$CLEANUP_RESULT" != "PASS" ]]; then
    overall=FAIL
  fi
  printf '\n=== MC-5 GATE 2 RESULT ===\n'
  printf 'OVERALL=%s\n' "$overall"
  printf 'PASS_COUNT=%s\n' "$PASS_COUNT"
  printf 'FAIL_COUNT=%s\n' "$FAIL_COUNT"
  printf 'WARNING_COUNT=%s\n' "$WARN_COUNT"
  printf 'CLEANUP=%s\n' "$CLEANUP_RESULT"
  printf 'NETWORK_PROVIDER_CALLS=%s\n' "$NETWORK_CALL_COUNT"
  printf 'BROWSER=%s\n' "$BROWSER_RESULT"
  printf 'LIVE_DATABASE_MODIFIED=NO_SCRIPT_WRITE_ATTEMPTED\n'
  printf 'LIVE_CONFIGURATION_MODIFIED=NO_SCRIPT_WRITE_ATTEMPTED\n'
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
  local rc=$?
  cleanup
  print_summary "$rc"
  return "$rc"
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
  local live_meta_after live_hash_after response_file ack_status
  local scan_files=() api_path key

  printf '%s\n' '=== MC-5 GATE 2 TARGET-VPS STAGING ==='
  printf 'target_repo=%s\n' "$REPO"
  printf 'expected_commit=%s\n' "$EXPECTED_COMMIT"

  [[ "$PWD" == "$REPO" ]] || { record_fail TARGET_WORKING_DIRECTORY "run from $REPO"; return 1; }
  [[ -d "$REPO/.git" ]] || { record_fail TARGET_REPOSITORY_PRESENT 'repository is absent'; return 1; }
  current_head=$(git -C "$REPO" rev-parse HEAD)
  [[ "$current_head" == "$EXPECTED_COMMIT" ]] || { record_fail TARGET_COMMIT "expected $EXPECTED_COMMIT, found $current_head"; return 1; }
  record_pass TARGET_COMMIT "$current_head"
  current_status=$(git -C "$REPO" status --porcelain)
  [[ -z "$current_status" ]] || { record_fail TARGET_WORKTREE_CLEAN 'worktree is dirty'; return 1; }
  record_pass TARGET_WORKTREE_CLEAN
  [[ -x "$AIPM_CLI" && -x "$VENV_PYTHON" ]] || { record_fail TARGET_RUNTIME 'AIPM CLI or Python runtime is unavailable'; return 1; }
  "$AIPM_CLI" dashboard --help >/dev/null || { record_fail TARGET_CLI_REGISTRATION 'dashboard CLI registration/help failed'; return 1; }
  record_pass TARGET_RUNTIME
  record_pass TARGET_CLI_REGISTRATION
  require_command curl || return 1
  require_command stat || return 1
  require_command sha256sum || return 1
  require_command ss || return 1
  require_command pgrep || return 1
  require_command systemctl || return 1
  require_command mktemp || return 1

  [[ -f "$LIVE_CONFIG" ]] || { record_fail LIVE_CONFIG_PRESENT 'configuration file is absent'; return 1; }
  [[ -f "$LIVE_DB" ]] || { record_fail LIVE_DATABASE_PRESENT 'database file is absent'; return 1; }
  [[ "$VENV_PYTHON" -nt "$REPO/pyproject.toml" || -x "$VENV_PYTHON" ]] || { record_fail TARGET_VENV 'runtime check failed'; return 1; }

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

  [[ "$(systemctl --user is-active aipm-telemetry.service)" == active ]] || { record_fail TELEMETRY_ACTIVE 'telemetry service is not active'; return 1; }
  [[ "$(systemctl --user is-enabled aipm-telemetry.service)" == enabled ]] || { record_fail TELEMETRY_ENABLED 'telemetry service is not enabled'; return 1; }
  [[ "$(systemctl --user is-active aipm-events.service)" == active ]] || { record_fail MC3_ACTIVE 'MC-3 event service is not active'; return 1; }
  [[ "$(systemctl --user is-enabled aipm-events.service)" == enabled ]] || { record_fail MC3_ENABLED 'MC-3 event service is not enabled'; return 1; }
  record_pass TELEMETRY_MC3_SERVICES 'active and enabled'

  PROCESS_STATE_BEFORE=$(mktemp)
  EXTRA_TEMP_FILES+=("$PROCESS_STATE_BEFORE")
  process_state "$PROCESS_STATE_BEFORE"
  grep -Eq '^notifications=0$' "$PROCESS_STATE_BEFORE" || { record_fail NOTIFICATION_WORKER_ABSENT 'notification worker is running'; rm -f "$PROCESS_STATE_BEFORE"; return 1; }
  record_pass NOTIFICATION_WORKER_ABSENT
  SERVICE_STATE_BEFORE=$(mktemp)
  EXTRA_TEMP_FILES+=("$SERVICE_STATE_BEFORE")
  service_state "$SERVICE_STATE_BEFORE"
  record_pass PRODUCTION_SERVICE_BASELINE

  LIVE_DB_META_BEFORE=$(safe_db_meta "$LIVE_DB")
  LIVE_DB_HASH_BEFORE=$(hash_db "$LIVE_DB")
  printf 'live_db_meta_before=%s\n' "$LIVE_DB_META_BEFORE"
  printf 'live_db_sha256_before=%s\n' "$LIVE_DB_HASH_BEFORE"
  record_pass LIVE_DATABASE_METADATA_CAPTURED

  STAGE_ROOT=$(mktemp -d -p /tmp aipm-mc5-gate2-XXXXXX)
  TEMP_CREATED=1
  chmod 700 "$STAGE_ROOT"
  STAGE_DB="$STAGE_ROOT/mission_control.db"
  STAGE_CONFIG="$STAGE_ROOT/config.yaml"
  write_network_guard
  record_pass TEMPORARY_STATE_CREATED

  if ! "$VENV_PYTHON" - "$LIVE_DB" "$STAGE_DB" <<'PY'
import sqlite3
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2]).resolve()
with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(destination) as dst:
    src.backup(dst)
PY
  then
    record_fail TEMPORARY_SQLITE_BACKUP 'SQLite backup API failed'
    return 1
  fi
  record_pass TEMPORARY_SQLITE_BACKUP
  stage_integrity "$STAGE_DB" || { record_fail TEMPORARY_SQLITE_INTEGRITY 'temporary backup failed integrity or foreign-key check'; return 1; }
  record_pass TEMPORARY_SQLITE_INTEGRITY

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
  channels: {}
  policies: {}
projects:
  search_paths: []
YAML
  chmod 600 "$STAGE_CONFIG"
  if ! safe_notification_state "$STAGE_CONFIG" "$STAGE_ROOT/stage-notifications.txt"; then
    record_fail STAGING_NOTIFICATIONS_DISABLED 'temporary ConfigManager validation failed'
    return 1
  fi
  grep -q '^enabled=False$' "$STAGE_ROOT/stage-notifications.txt" || { record_fail STAGING_NOTIFICATIONS_DISABLED 'temporary effective value is not False'; return 1; }
  record_pass STAGING_NOTIFICATIONS_DISABLED
  grep -Eq '^channel_count=0$' "$STAGE_ROOT/stage-notifications.txt" && grep -Eq '^policy_count=0$' "$STAGE_ROOT/stage-notifications.txt" || { record_fail STAGING_NOTIFICATION_CHANNELS 'temporary channels or policies are not empty'; return 1; }
  record_pass STAGING_NOTIFICATION_CHANNELS

  DASH_PORT=$($VENV_PYTHON - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    print(sock.getsockname()[1])
PY
)
  NETWORK_GUARD_LOG="$STAGE_ROOT/network_guard/blocked.jsonl"
  mkdir -p "$STAGE_ROOT/home"
  env -i HOME="$STAGE_ROOT/home" PATH="$REPO/.venv/bin:/usr/bin:/bin" PYTHONPATH="$STAGE_ROOT/network_guard:$REPO/src" \
    AIPM_CONFIG="$STAGE_CONFIG" AIPM_TELEMETRY_DB="$STAGE_DB" \
    AIPM_MC5_NETWORK_GUARD_LOG="$NETWORK_GUARD_LOG" \
    "$AIPM_CLI" dashboard --host 127.0.0.1 --port "$DASH_PORT" >"$STAGE_ROOT/dashboard.log" 2>&1 &
  DASH_PID=$!
  for _ in $(seq 1 30); do
    if curl --fail --silent --show-error "http://127.0.0.1:$DASH_PORT/healthz" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  kill -0 "$DASH_PID" 2>/dev/null || { record_fail DASHBOARD_STARTUP 'dashboard process exited'; return 1; }
  curl --fail --silent --show-error "http://127.0.0.1:$DASH_PORT/healthz" >/dev/null || { record_fail DASHBOARD_HEALTH 'healthz failed'; return 1; }
  record_pass DASHBOARD_STARTUP
  record_pass DASHBOARD_HEALTH
  ss -ltn | grep -Eq "127\.0\.0\.1:${DASH_PORT}[[:space:]]" || { record_fail LOOPBACK_BIND "dashboard is not listening on 127.0.0.1:$DASH_PORT"; return 1; }
  ! ss -ltn | grep -Eq "0\.0\.0\.0:${DASH_PORT}|\[::\]:${DASH_PORT}" || { record_fail NON_LOOPBACK_BIND 'dashboard listener is not loopback-only'; return 1; }
  record_pass LOOPBACK_BIND "127.0.0.1:$DASH_PORT"

  STAGE_DB_HASH_BEFORE_READS=$(hash_db "$STAGE_DB")
  stage_integrity "$STAGE_DB" || { record_fail TEMPORARY_SQLITE_BEFORE_READS 'integrity check failed before API reads'; return 1; }
  record_pass TEMPORARY_SQLITE_BEFORE_READS

  local api_paths=(
    '/'
    '/api/services'
    '/api/overview'
    '/api/events'
    '/api/incidents'
    '/api/notifications'
    '/api/notification-channels'
    '/api/notification-policies'
    '/api/notification-metrics'
    '/api/history/host?range=1h&limit=10'
    '/api/history/containers?range=1h&limit=10'
    '/api/history/container-resources?range=1h&limit=10'
    '/api/history/projects?range=1h&limit=10'
    '/api/history/tunnel?range=1h&limit=10'
  )
  for api_path in "${api_paths[@]}"; do
    key=$(api_filename "$api_path")
    if [[ "$api_path" == "/" ]]; then
      response_file="$STAGE_ROOT/response-$key"
    else
      response_file="$STAGE_ROOT/response-$key.json"
    fi
    if ! curl --fail --silent --show-error --request GET "http://127.0.0.1:$DASH_PORT$api_path" -o "$response_file"; then
      record_fail "API_$key" 'GET request failed'
      return 1
    fi
    scan_files+=("$response_file")
    record_pass "API_$key" 'GET 200'
  done

  ack_status=$(curl --silent --output /dev/null --write-out '%{http_code}' --request POST "http://127.0.0.1:$DASH_PORT/api/incidents/1/acknowledge")
  [[ "$ack_status" == 404 ]] || { record_fail ACK_ROUTE_ABSENT "expected 404, received $ack_status"; return 1; }
  record_pass ACK_ROUTE_ABSENT 'POST returned 404'

  write_response_scan
  if ! "$VENV_PYTHON" "$STAGE_ROOT/scan_responses.py" "${scan_files[@]}" >"$STAGE_ROOT/secret-scan.txt"; then
    record_fail RESPONSE_SECRET_SCAN 'secret-like response field or external URL detected; details withheld'
    return 1
  fi
  record_pass RESPONSE_SECRET_SCAN 'no secret-like fields or external URLs'

  if [[ -s "$NETWORK_GUARD_LOG" ]]; then
    NETWORK_CALL_COUNT=$(wc -l <"$NETWORK_GUARD_LOG")
    record_fail NETWORK_PROVIDER_CALLS "blocked non-loopback network attempts=$NETWORK_CALL_COUNT"
    return 1
  fi
  NETWORK_CALL_COUNT=0
  record_pass NETWORK_PROVIDER_CALLS 'zero blocked non-loopback attempts'

  STAGE_DB_HASH_AFTER_READS=$(hash_db "$STAGE_DB")
  [[ "$STAGE_DB_HASH_BEFORE_READS" == "$STAGE_DB_HASH_AFTER_READS" ]] || { record_fail TEMPORARY_SQLITE_STABILITY 'dashboard reads changed the temporary database'; return 1; }
  record_pass TEMPORARY_SQLITE_STABILITY
  stage_integrity "$STAGE_DB" || { record_fail TEMPORARY_SQLITE_AFTER_READS 'integrity check failed after API reads'; return 1; }
  record_pass TEMPORARY_SQLITE_AFTER_READS

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

  LIVE_DB_META_AFTER=$(safe_db_meta "$LIVE_DB")
  LIVE_DB_HASH_AFTER=$(hash_db "$LIVE_DB")
  [[ "$LIVE_DB_META_BEFORE" == "$LIVE_DB_META_AFTER" ]] || { record_fail LIVE_DATABASE_METADATA_UNCHANGED 'live database metadata changed'; return 1; }
  [[ "$LIVE_DB_HASH_BEFORE" == "$LIVE_DB_HASH_AFTER" ]] || { record_fail LIVE_DATABASE_HASH_UNCHANGED 'live database SHA-256 changed'; return 1; }
  record_pass LIVE_DATABASE_UNCHANGED

  SERVICE_STATE_AFTER=$(mktemp)
  EXTRA_TEMP_FILES+=("$SERVICE_STATE_AFTER")
  service_state "$SERVICE_STATE_AFTER"
  cmp -s "$SERVICE_STATE_BEFORE" "$SERVICE_STATE_AFTER" || { record_fail TELEMETRY_MC3_SERVICES_UNCHANGED 'service state changed'; return 1; }
  record_pass TELEMETRY_MC3_SERVICES_UNCHANGED
  PROCESS_STATE_AFTER=$(mktemp)
  EXTRA_TEMP_FILES+=("$PROCESS_STATE_AFTER")
  process_state "$PROCESS_STATE_AFTER"
  cmp -s "$PROCESS_STATE_BEFORE" "$PROCESS_STATE_AFTER" || { record_fail TELEMETRY_MC3_PROCESSES_UNCHANGED 'process counts changed'; return 1; }
  grep -Eq '^notifications=0$' "$PROCESS_STATE_AFTER" || { record_fail NOTIFICATION_WORKER_ABSENT_AFTER 'notification worker is running after staging'; return 1; }
  record_pass TELEMETRY_MC3_PROCESSES_UNCHANGED

  NOTIFICATIONS_AFTER=$(mktemp)
  EXTRA_TEMP_FILES+=("$NOTIFICATIONS_AFTER")
  if ! safe_notification_state "$LIVE_CONFIG" "$NOTIFICATIONS_AFTER"; then
    record_fail LIVE_NOTIFICATIONS_AFTER 'live ConfigManager validation failed'; return 1
  fi
  cmp -s "$NOTIFICATIONS_BEFORE" "$NOTIFICATIONS_AFTER" || { record_fail LIVE_NOTIFICATIONS_UNCHANGED 'safe notification state changed'; return 1; }
  grep -q '^enabled=False$' "$NOTIFICATIONS_AFTER" || { record_fail LIVE_NOTIFICATIONS_AFTER 'effective notifications.enabled is not False'; return 1; }
  record_pass LIVE_NOTIFICATIONS_UNCHANGED 'safe notification state unchanged and effective value remains False'

  record_pass CLEANUP_READY
  return 0
}

main "$@"
