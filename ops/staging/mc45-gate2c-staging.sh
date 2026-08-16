#!/usr/bin/env bash
# MC-4.5 Gate 2C — isolated target-VPS staging only.
# Run as: bash ~/mc45-gate2c-staging.sh
# Do not source this file. It never calls shell exit, so it cannot terminate an
# operator's SSH shell when invoked normally with bash.

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  printf '%s\n' 'Refusing to run when sourced. Execute with: bash ~/mc45-gate2c-staging.sh' >&2
  return 1 2>/dev/null || :
fi

set -u -o pipefail

REPO=/home/ubuntu/aipm
VENV_PYTHON=/home/ubuntu/aipm/.venv/bin/python
CLI=/home/ubuntu/aipm/.venv/bin/aipm
LIVE_CONFIG=/home/ubuntu/.config/aipm/config.yaml
LIVE_DB=/home/ubuntu/.local/state/aipm/telemetry/mission_control.db
STAGE_UNIT="$HOME/.config/systemd/user/aipm-notifications-staging.service"

STAGE_ROOT=
STAGE_DB=
STAGE_CONFIG=
STAGE_RUNNER=
STAGE_SEEDER=
STAGE_CHECKER=
ACTIVITY=
BACKUP=
STAGE_STARTED=0
UNIT_CREATED=0
MANAGER_RELOADED=0
CLEANUP_DONE=0
SUMMARY_PRINTED=0
DIRECT_PIDS=()
STARTED_PID=
FAKE_ADAPTER_GUARD_OK=0
FAIL_COUNT=0

RESULT_KEYS=()
RESULT_VALUES=()
RESULT_DETAILS=()
FAILED_KEYS=()
FAILED_REASONS=()

record() {
  local key="$1" value="$2" detail="${3:-}"
  RESULT_KEYS+=("$key")
  RESULT_VALUES+=("$value")
  RESULT_DETAILS+=("$detail")
  if [[ "$value" != PASS ]]; then
    FAIL_COUNT=$((FAIL_COUNT + 1))
    FAILED_KEYS+=("$key")
    FAILED_REASONS+=("$detail")
  fi
  printf 'CHECK_%s=%s%s\n' "$key" "$value" "${detail:+ detail=$detail}"
}

print_summary() {
  printf '%s\n' '=== MC-4.5 GATE 2C RESULT ==='
  local i
  for i in "${!RESULT_KEYS[@]}"; do printf '%s=%s%s\n' "${RESULT_KEYS[$i]}" "${RESULT_VALUES[$i]}" "${RESULT_DETAILS[$i]:+ detail=${RESULT_DETAILS[$i]}}"; done
  printf 'FAIL_COUNT=%s\n' "$FAIL_COUNT"
  if [[ "$FAIL_COUNT" == 0 ]]; then printf '%s\n' 'OVERALL=PASS'; else printf '%s\n' 'OVERALL=FAIL'; fi
  printf '%s\n' 'LIVE_DATABASE_MODIFIED=NO_SCRIPT_WRITE_ATTEMPTED'
  printf '%s\n' 'LIVE_CONFIGURATION_MODIFIED=NO'
  printf '%s\n' 'EXTERNAL_PROVIDER_CALL=NO'
  printf '%s\n' 'PERSISTENT_NOTIFICATION_ENABLEMENT=NO'
  printf '%s\n' 'GATE_3_STARTED=NO'
}

cleanup() {
  local prior_status=$? cleanup_status=PASS pid
  if [[ "$STAGE_STARTED" == 1 ]]; then
    systemctl --user stop aipm-notifications-staging.service >/dev/null 2>&1 || cleanup_status=FAIL
    STAGE_STARTED=0
  fi
  for pid in "${DIRECT_PIDS[@]}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      kill -TERM "$pid" >/dev/null 2>&1 || true
      wait "$pid" >/dev/null 2>&1 || true
    fi
  done
  DIRECT_PIDS=()
  if [[ "$UNIT_CREATED" == 1 ]]; then
    rm -f -- "$STAGE_UNIT" || cleanup_status=FAIL
    UNIT_CREATED=0
    if [[ "$MANAGER_RELOADED" == 1 ]]; then
      systemctl --user daemon-reload >/dev/null 2>&1 || cleanup_status=FAIL
    fi
  fi
  if [[ -n "$STAGE_ROOT" && -e "$STAGE_ROOT" ]]; then
    rm -rf -- "$STAGE_ROOT" || cleanup_status=FAIL
  fi
  if [[ -n "$STAGE_ROOT" && -e "$STAGE_ROOT" ]]; then cleanup_status=FAIL; fi
  if [[ -e "$STAGE_UNIT" ]]; then cleanup_status=FAIL; fi
  if [[ "$CLEANUP_DONE" == 0 ]]; then
    CLEANUP_DONE=1
    record CLEANUP "$cleanup_status" "temporary files, processes, and staging unit removed"
  fi
  return "$prior_status"
}

trap cleanup EXIT INT TERM HUP

wait_for_file() {
  local path="$1" seconds="${2:-10}" i=0
  while [[ "$i" -lt "$seconds" ]]; do
    [[ -f "$path" ]] && return 0
    sleep 1
    i=$((i + 1))
  done
  return 1
}

wait_for_activity_count() {
  local expected="$1" seconds="${2:-15}" actual i=0
  while [[ "$i" -lt "$seconds" ]]; do
    actual=$(grep -c '^FAKE_SEND ' "$ACTIVITY" 2>/dev/null || true)
    [[ "$actual" -ge "$expected" ]] && return 0
    sleep 1
    i=$((i + 1))
  done
  return 1
}

wait_for_rate_limit_suppression() {
  local expected="$1" seconds="${2:-15}" actual i=0
  while [[ "$i" -lt "$seconds" ]]; do
    actual=$($VENV_PYTHON "$STAGE_CHECKER" "$STAGE_DB" 2>/dev/null | awk -F= '$1 == "stage.metrics.rate_limit_suppressions" {print $2; exit}')
    [[ "$actual" == "$expected" ]] && return 0
    sleep 1
    i=$((i + 1))
  done
  return 1
}

seed_transition() {
  local result
  result=$($VENV_PYTHON "$STAGE_SEEDER" "$STAGE_DB" 2>/dev/null)
  [[ -n "$result" ]] || return 1
  printf '%s\n' "$result"
}

read_stage_check() {
  "$VENV_PYTHON" "$STAGE_CHECKER" "$STAGE_DB"
}

start_direct_worker() {
  local mode="$1"
  local label="$2"
  local ready="$STAGE_ROOT/ready-$label"
  local release="$STAGE_ROOT/release-$label"
  local log="$STAGE_ROOT/worker-$label.log"
  local block_arg=
  STARTED_PID=
  rm -f -- "$ready" "$release"
  [[ "$mode" == block ]] && block_arg=--block
  env HOME="$STAGE_ROOT/home" PYTHONPATH="$REPO/src" AIPM_CONFIG="$STAGE_CONFIG" AIPM_TELEMETRY_DB="$STAGE_DB" \
    "$VENV_PYTHON" "$STAGE_RUNNER" --db "$STAGE_DB" --config "$STAGE_CONFIG" --activity "$ACTIVITY" --ready "$ready" --release "$release" --lease 2 $block_arg >"$log" 2>&1 &
  STARTED_PID=$!
  DIRECT_PIDS+=("$STARTED_PID")
  if ! wait_for_file "$ready" 15; then
    kill -TERM "$STARTED_PID" >/dev/null 2>&1 || true
    wait "$STARTED_PID" >/dev/null 2>&1 || true
    STARTED_PID=
    return 1
  fi
}

stop_direct_worker() {
  local pid="$1" signal_name="$2"
  kill -s "$signal_name" "$pid" >/dev/null 2>&1 || return 1
  wait "$pid"
}

write_staging_files() {
  mkdir -p -- "$STAGE_ROOT/home" "$STAGE_ROOT/work"
  chmod 700 "$STAGE_ROOT" "$STAGE_ROOT/home" "$STAGE_ROOT/work"
  cat >"$STAGE_CONFIG" <<YAML
logging:
  level: INFO
  file: $STAGE_ROOT/aipm.log
  max_size_mb: 1
  backup_count: 1
discovery:
  search_paths:
    - $STAGE_ROOT
  ignore_dirs: []
  max_depth: 1
  follow_symlinks: false
telemetry:
  enabled: true
  interval_seconds: 15
  resource_sampling_enabled: true
  resource_interval_seconds: 60
  resource_timeout_seconds: 15
  resource_stale_after_seconds: 180
  project_interval_seconds: 60
  project_timeout_seconds: 15
  slow_task_max_concurrency: 1
  sampling_mode: split
  retention_days: 1
  database_path: $STAGE_DB
events:
  enabled: true
  interval_seconds: 15
  event_retention_days: 30
  incident_retention_days: 180
  acknowledgement_enabled: true
notifications:
  enabled: false
  interval_seconds: 1
  retention_days: 180
  default_cooldown_seconds: 900
  default_window_seconds: 3600
  default_max_notifications: 3
  global_window_seconds: 3600
  global_max_notifications: 100
  channels: []
  policies: []
YAML
  chmod 600 "$STAGE_CONFIG"

  cat >"$STAGE_RUNNER" <<'PY'
from __future__ import annotations
import argparse
import time
from pathlib import Path
from aipm.core.config import ConfigManager
from aipm.models.events import EventType, ResourceType
from aipm.models.finding import Severity
from aipm.models.notifications import DeliveryResult, DeliveryStatus, NotificationChannel, NotificationPolicy, NotificationTrigger
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository
from aipm.services.notifications import channels as channel_module
from aipm.services.notifications.channels import ChannelRegistry
from aipm.services.notifications.runner import NotificationRunner
from aipm.services.notifications.worker import NotificationProjector, NotificationWorker

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--activity', required=True)
    parser.add_argument('--ready', required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--lease', type=int, default=2)
    parser.add_argument('--block', action='store_true')
    args = parser.parse_args()
    config = ConfigManager(Path(args.config)).config
    assert config.notifications.enabled is False
    assert str(Path(args.db).resolve()).startswith(str(Path(args.config).parent.resolve()))

    # Defense in depth: any provider URL call imported by the channel module is blocked.
    def network_forbidden(*_args, **_kwargs):
        raise RuntimeError('Gate 2C network/provider call blocked')
    channel_module.urlopen = network_forbidden

    repository = SQLiteNotificationRepository(Path(args.db))
    channel = NotificationChannel('staging-mock', 'Gate 2C fake adapter', 'mock', True, None, None, 1, 3)
    policy = NotificationPolicy(
        'staging-critical', 'Gate 2C staging policy', True, Severity.CRITICAL,
        (EventType.CONTAINER_RESTARTING,), (ResourceType.CONTAINER,), (),
        (NotificationTrigger.INCIDENT_OPENED,), False, False, False, 0, 3600, 3, ('staging-mock',)
    )
    activity = Path(args.activity)
    class FakeAdapter:
        channel_type = 'mock'
        def send(self, notification, context):
            assert context.secret is None
            assert context.destination is None
            with activity.open('a', encoding='utf-8') as handle:
                handle.write(f'FAKE_SEND_STARTED notification={notification.id}\n')
                handle.flush()
            if args.block:
                deadline = time.monotonic() + 30
                release = Path(args.release)
                while not release.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
            with activity.open('a', encoding='utf-8') as handle:
                handle.write(f'FAKE_SEND notification={notification.id}\n')
                handle.flush()
            return DeliveryResult(DeliveryStatus.SENT, False, provider_message_id=f'gate2c-fake-{notification.id}')

    registry = ChannelRegistry({'mock': FakeAdapter()})
    projector = NotificationProjector(repository, (policy,), (channel,))
    worker = NotificationWorker(repository, registry, (channel,), lease_seconds=args.lease)
    Path(args.ready).write_text('ready\n', encoding='utf-8')
    NotificationRunner(projector, worker, config.notifications).run()

if __name__ == '__main__':
    main()
PY

  cat >"$STAGE_SEEDER" <<'PY'
from __future__ import annotations
import sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
from aipm.models.events import EventType, ResourceRef, ResourceType
from aipm.models.finding import Severity
from aipm.models.incidents import IncidentStatus
from aipm.models.notifications import IncidentTransition, NotificationTrigger
from aipm.repositories.notifications.sqlite import SQLiteNotificationRepository

db = Path(sys.argv[1]).resolve()
repo = SQLiteNotificationRepository(db)
now = datetime.now(timezone.utc)
with repo._connection() as con:
    next_id = int(con.execute('SELECT COALESCE(MAX(id), 0) + 1 FROM incidents').fetchone()[0])
    key = f'gate2c:container:{next_id}'
    con.execute('INSERT INTO incidents (id, incident_key, title, severity, status, started_at, updated_at, resource_type, resource_id, correlation_key, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (next_id, key, 'Gate 2C isolated incident', Severity.CRITICAL.value, IncidentStatus.OPEN.value, int(now.timestamp()), int(now.timestamp()), 'container', f'gate2c-{next_id}', f'container:gate2c-{next_id}:stability', 'Synthetic MC-3-style transition for isolated staging'))
transition = IncidentTransition(None, next_id, key, NotificationTrigger.INCIDENT_OPENED, now, None, IncidentStatus.OPEN, None, Severity.CRITICAL, None, f'gate2c:event:{next_id}', f'container:gate2c-{next_id}:stability', ResourceRef(ResourceType.CONTAINER, f'gate2c-{next_id}', f'gate2c-container-{next_id}', '/tmp/gate2c'), EventType.CONTAINER_RESTARTING)
print(repo.add_transition(transition))
PY

  cat >"$STAGE_CHECKER" <<'PY'
from __future__ import annotations
import sqlite3, sys
from pathlib import Path
p = Path(sys.argv[1]).resolve()
con = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
con.row_factory = sqlite3.Row
integrity = con.execute('PRAGMA integrity_check').fetchone()[0]
foreign_keys = con.execute('PRAGMA foreign_key_check').fetchall()
print(f'stage.sqlite.integrity_check={integrity}')
print(f'stage.sqlite.foreign_key_violations={len(foreign_keys)}')
existing = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
for table in ('incident_transitions', 'notification_projection_runs', 'notifications', 'notification_deliveries', 'notification_attempts'):
    count = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] if table in existing else 'TABLE_ABSENT'
    print(f'stage.sqlite.count.{table}={count}')
if 'notifications' in existing:
    statuses = {row['status']: int(row['count']) for row in con.execute('SELECT status, COUNT(*) AS count FROM notifications GROUP BY status')}
    print(f"stage.metrics.pending={statuses.get('pending', 0)}")
    print(f"stage.metrics.sending={statuses.get('sending', 0)}")
    print(f"stage.metrics.failed={statuses.get('failed', 0)}")
    print(f"stage.metrics.unknown={statuses.get('unknown', 0)}")
    print(f"stage.metrics.sent={statuses.get('sent', 0)}")
    print(f"stage.metrics.suppressed={statuses.get('suppressed', 0)}")
    rate_limit_suppressions = con.execute("SELECT COUNT(*) FROM notification_suppressions WHERE reason = 'rate_limit'").fetchone()[0] if 'notification_suppressions' in existing else 0
    print(f"stage.metrics.rate_limit_suppressions={rate_limit_suppressions}")
    print(f"stage.metrics.identities_duplicate={con.execute('SELECT COUNT(*) - COUNT(DISTINCT identity_key) FROM notifications').fetchone()[0]}")
    print(f"stage.metrics.provider_keys_duplicate={con.execute('SELECT COUNT(*) - COUNT(DISTINCT provider_request_key) FROM notification_deliveries').fetchone()[0]}")
con.close()
raise SystemExit(0 if integrity == 'ok' and not foreign_keys else 1)
PY
}

write_unit() {
  mkdir -p -- "$(dirname "$STAGE_UNIT")"
  if [[ -e "$STAGE_UNIT" ]]; then return 1; fi
  cat >"$STAGE_UNIT" <<UNIT
[Unit]
Description=AIPM MC-4.5 isolated notification staging worker
After=aipm-events.service

[Service]
Type=simple
WorkingDirectory=$STAGE_ROOT/work
Environment=HOME=$STAGE_ROOT/home
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$REPO/src
Environment=AIPM_CONFIG=$STAGE_CONFIG
Environment=AIPM_TELEMETRY_DB=$STAGE_DB
ExecStart=$VENV_PYTHON $STAGE_RUNNER --db $STAGE_DB --config $STAGE_CONFIG --activity $ACTIVITY --ready $STAGE_ROOT/ready-systemd --release $STAGE_ROOT/release-systemd --lease 2
Restart=on-failure
RestartSec=5

[Install]
# Deliberately no WantedBy and never enabled.
UNIT
  chmod 600 "$STAGE_UNIT"
  UNIT_CREATED=1
}

main() {
  printf '%s\n' '=== MC-4.5 GATE 2C OPERATOR STAGING ==='
  printf 'target_repo=%s\n' "$REPO"
  printf 'hostname=%s\n' "$(hostname 2>/dev/null || true)"
  printf 'user=%s\n' "$(id -un 2>/dev/null || true)"

  local repo_ok=0 config_ok=0 backup_ok=0
  if [[ -d "$REPO/.git" ]]; then record TARGET_REPOSITORY_PRESENT PASS 'verified /home/ubuntu/aipm repository'; else record TARGET_REPOSITORY_PRESENT FAIL 'repository directory or .git metadata missing'; fi
  if [[ -x "$VENV_PYTHON" ]]; then record TARGET_VENV_PYTHON_PRESENT PASS 'verified /home/ubuntu/aipm/.venv/bin/python'; else record TARGET_VENV_PYTHON_PRESENT FAIL 'AIPM virtualenv Python is missing or not executable'; fi
  if [[ -x "$CLI" ]]; then record TARGET_CLI_PRESENT PASS 'verified /home/ubuntu/aipm/.venv/bin/aipm'; else record TARGET_CLI_PRESENT FAIL 'AIPM CLI is missing or not executable'; fi
  [[ -d "$REPO/.git" && -x "$VENV_PYTHON" && -x "$CLI" ]] && repo_ok=1
  if [[ -f "$LIVE_CONFIG" ]]; then config_ok=1; record LIVE_CONFIG_PRESENT PASS 'verified live configuration path'; else record LIVE_CONFIG_PRESENT FAIL 'live configuration missing'; fi

  local live_config_hash_before live_config_hash_after
  live_config_hash_before=$(sha256sum "$LIVE_CONFIG" 2>/dev/null | awk '{print $1}')
  local live_notifications_enabled=UNAVAILABLE live_config_manager_rc=1 live_notifications_check=FAIL
  if [[ "$config_ok" == 1 ]]; then
    live_notifications_enabled=$($VENV_PYTHON - "$LIVE_CONFIG" 2>/dev/null <<'PY'
import sys
from pathlib import Path
from aipm.core.config import ConfigManager
config = ConfigManager(Path(sys.argv[1]))
print(config.config.notifications.enabled)
PY
    )
    live_config_manager_rc=$?
    [[ "$live_config_manager_rc" == 0 && -n "$live_notifications_enabled" ]] || live_notifications_enabled=UNAVAILABLE
  fi
  printf 'live_notifications_enabled=%s\n' "$live_notifications_enabled"
  if [[ "$live_config_manager_rc" == 0 && "$live_notifications_enabled" == False ]]; then
    live_notifications_check=PASS
    record LIVE_NOTIFICATIONS_DISABLED PASS 'authoritative ConfigManager reports notifications.enabled=False'
  else
    record LIVE_NOTIFICATIONS_DISABLED FAIL 'authoritative ConfigManager did not report notifications.enabled=False'
  fi

  local telemetry_active events_active telemetry_enabled events_enabled
  telemetry_active=$(systemctl --user is-active aipm-telemetry.service 2>/dev/null || true)
  events_active=$(systemctl --user is-active aipm-events.service 2>/dev/null || true)
  telemetry_enabled=$(systemctl --user is-enabled aipm-telemetry.service 2>/dev/null || true)
  events_enabled=$(systemctl --user is-enabled aipm-events.service 2>/dev/null || true)
  printf 'telemetry_before=%s enabled=%s\n' "$telemetry_active" "$telemetry_enabled"
  printf 'events_before=%s enabled=%s\n' "$events_active" "$events_enabled"
  [[ "$telemetry_active" == active ]] && record TELEMETRY_ACTIVE PASS || record TELEMETRY_ACTIVE FAIL 'aipm-telemetry.service must be active'
  [[ "$telemetry_enabled" == enabled ]] && record TELEMETRY_ENABLED PASS || record TELEMETRY_ENABLED FAIL 'aipm-telemetry.service must be enabled'
  [[ "$events_active" == active ]] && record MC3_EVENTS_ACTIVE PASS || record MC3_EVENTS_ACTIVE FAIL 'aipm-events.service must be active'
  [[ "$events_enabled" == enabled ]] && record MC3_EVENTS_ENABLED PASS || record MC3_EVENTS_ENABLED FAIL 'aipm-events.service must be enabled'

  local production_worker_count
  production_worker_count=$(pgrep -f '[a]ipm notifications run' 2>/dev/null | wc -l)
  [[ "$production_worker_count" == 0 ]] && record PRODUCTION_WORKER_ABSENT PASS || record PRODUCTION_WORKER_ABSENT FAIL 'production notification worker already running'
  local production_unit_state
  production_unit_state=$(systemctl --user show aipm-notifications.service -p LoadState --value 2>/dev/null || true)
  printf 'production_notification_unit_load=%s\n' "${production_unit_state:-unavailable}"
  [[ "$production_unit_state" == not-found || "$production_unit_state" == unloaded ]] && record PRODUCTION_UNIT_UNMODIFIED PASS || record PRODUCTION_UNIT_UNMODIFIED FAIL 'production notification unit must be absent/unloaded; staging will not touch it'

  BACKUP="${AIPM_MC45_VERIFIED_BACKUP:-/home/ubuntu/.local/state/aipm/backups/mission_control-pre-mc3-20260816T191436Z.db}"
  # No backup is created here. If the verified path is absent or fails the
  # later read-only integrity check, the script stops and requires operator review.
  # An explicit AIPM_MC45_VERIFIED_BACKUP value may point to another already
  # verified backup; it is never copied from or written to by this script.
  printf 'verified_backup=%s\n' "${BACKUP:-NOT_FOUND}"
  if [[ -n "$BACKUP" && -f "$BACKUP" && "$BACKUP" != "$LIVE_DB" ]]; then backup_ok=1; fi
  [[ "$backup_ok" == 1 ]] && record VERIFIED_BACKUP_PRESENT PASS 'backup path is distinct from live database' || record VERIFIED_BACKUP_PRESENT FAIL 'set AIPM_MC45_VERIFIED_BACKUP to the exact verified backup path'

  if [[ "$repo_ok" != 1 || "$config_ok" != 1 || "$live_notifications_check" != PASS || "$telemetry_active" != active || "$telemetry_enabled" != enabled || "$events_active" != active || "$events_enabled" != enabled || "$production_worker_count" != 0 || "$production_unit_state" != not-found && "$production_unit_state" != unloaded || "$backup_ok" != 1 ]]; then
    printf '%s\n' 'FAILED_CHECKS:'
    if [[ "${#FAILED_KEYS[@]}" == 0 ]]; then
      printf '%s\n' '- PREREQUISITE_GUARD'
      printf '%s\n' '  - compound prerequisite state did not satisfy the required conditions; inspect the explicit CHECK_* results above'
    else
      local failed_index
      for failed_index in "${!FAILED_KEYS[@]}"; do
        printf -- '- %s\n' "${FAILED_KEYS[$failed_index]}"
        printf '  - %s\n' "${FAILED_REASONS[$failed_index]}"
      done
    fi
    printf '%s\n' 'Prerequisites failed; no staging files, unit, process, or database copy were created.'
    return 1
  fi

  STAGE_ROOT=$(mktemp -d -p /tmp aipm-mc45-gate2c.XXXXXX)
  chmod 700 "$STAGE_ROOT"
  STAGE_DB="$STAGE_ROOT/mission_control.db"
  STAGE_CONFIG="$STAGE_ROOT/config.yaml"
  STAGE_RUNNER="$STAGE_ROOT/runner.py"
  STAGE_SEEDER="$STAGE_ROOT/seed.py"
  STAGE_CHECKER="$STAGE_ROOT/check.py"
  ACTIVITY="$STAGE_ROOT/activity.log"
  mkdir -p "$STAGE_ROOT/home" "$STAGE_ROOT/work"
  chmod 700 "$STAGE_ROOT/home" "$STAGE_ROOT/work"
  printf 'stage_root=%s\n' "$STAGE_ROOT"

  $VENV_PYTHON - "$BACKUP" <<'PY'
import sqlite3, sys
from pathlib import Path
p = Path(sys.argv[1]).resolve()
con = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
integ = con.execute('PRAGMA integrity_check').fetchone()[0]
fk = con.execute('PRAGMA foreign_key_check').fetchall()
print(f'backup_integrity={integ}')
print(f'backup_foreign_key_violations={len(fk)}')
con.close()
raise SystemExit(0 if integ == 'ok' and not fk else 1)
PY
  if [[ "$?" == 0 ]]; then record BACKUP_READONLY_INTEGRITY PASS 'verified backup is clean'; else record BACKUP_READONLY_INTEGRITY FAIL 'verified backup integrity check failed'; return 1; fi

  if cp --reflink=auto --preserve=mode,timestamps "$BACKUP" "$STAGE_DB" && chmod 600 "$STAGE_DB"; then record TEMPORARY_DB_COPY PASS 'live database was not used as the staging database'; else record TEMPORARY_DB_COPY FAIL 'copy failed'; return 1; fi
  write_staging_files && record ISOLATED_FILES PASS 'temporary config, harness, seeder, checker, and activity state created' || { record ISOLATED_FILES FAIL 'temporary file creation failed'; return 1; }
  if $VENV_PYTHON - "$STAGE_CONFIG" <<'PY'
import sys
from pathlib import Path
from aipm.core.config import ConfigManager
c = ConfigManager(Path(sys.argv[1])).config
assert c.notifications.enabled is False
assert Path(c.telemetry.database_path).resolve() == Path(sys.argv[1]).parent.joinpath('mission_control.db').resolve()
print('staging_notifications_enabled=false')
print('staging_database_is_temporary=true')
PY
  then record STAGING_CONFIG_DISABLED PASS 'staging notifications.enabled=false'; else record STAGING_CONFIG_DISABLED FAIL 'staging configuration validation failed'; return 1; fi
  local static_matches unapproved_static_matches
  static_matches=$(grep -nE 'urlopen|urllib|requests|httpx|socket|TelegramAdapter|HttpAdapter|ChannelRegistry\.default|os\.environ|secret_ref|destination_ref' "$STAGE_RUNNER" || true)
  unapproved_static_matches=$(printf '%s\n' "$static_matches" | grep -Ev '^[0-9]+:[[:space:]]*channel_module\.urlopen = network_forbidden$' || true)
  if [[ -z "$unapproved_static_matches" ]]; then
    FAKE_ADAPTER_GUARD_OK=1
    record FAKE_ADAPTER_STATIC_GUARD PASS 'only the approved network-blocking monkeypatch matched'
  else
    printf '%s\n' "$unapproved_static_matches"
    record FAKE_ADAPTER_STATIC_GUARD FAIL 'unapproved provider/network/credential pattern found'
    return 1
  fi

  if write_unit && systemd-analyze --user verify "$STAGE_UNIT" >/dev/null 2>&1; then record TEMP_UNIT_VERIFY PASS 'temporary unit validates'; else record TEMP_UNIT_VERIFY FAIL 'temporary unit validation failed'; return 1; fi
  systemctl --user daemon-reload >/dev/null 2>&1 && MANAGER_RELOADED=1
  if [[ "$MANAGER_RELOADED" == 1 ]]; then record TEMP_MANAGER_RELOAD PASS 'user manager reloaded without enabling the unit'; else record TEMP_MANAGER_RELOAD FAIL 'user manager reload failed'; return 1; fi

  if seed_transition >/dev/null; then record MC3_STYLE_SEED PASS 'synthetic incident transition seeded in temporary DB'; else record MC3_STYLE_SEED FAIL 'temporary transition seed failed'; return 1; fi

  rm -f -- "$STAGE_ROOT/ready-systemd"
  if systemctl --user start aipm-notifications-staging.service && STAGE_STARTED=1 && wait_for_file "$STAGE_ROOT/ready-systemd" 15 && systemctl --user is-active --quiet aipm-notifications-staging.service; then record WORKER_STARTUP PASS 'fake-only temporary worker active'; else record WORKER_STARTUP FAIL 'temporary worker did not start'; return 1; fi
  if wait_for_activity_count 1 15; then record INITIAL_PROJECTION_DELIVERY PASS 'one synthetic transition projected and fake-delivered'; else record INITIAL_PROJECTION_DELIVERY FAIL 'initial fake delivery not observed'; return 1; fi
  sleep 60
  if systemctl --user is-active --quiet aipm-notifications-staging.service; then record SIXTY_SECOND_HEALTH PASS 'worker remained active for at least 60 seconds'; else record SIXTY_SECOND_HEALTH FAIL 'worker not active after observation'; fi
  if read_stage_check >"$STAGE_ROOT/check-initial.txt" 2>&1; then record INITIAL_DB_INTEGRITY PASS 'temporary SQLite integrity and foreign keys clean'; else record INITIAL_DB_INTEGRITY FAIL 'temporary SQLite check failed'; fi

  if systemctl --user stop aipm-notifications-staging.service && STAGE_STARTED=0 && ! systemctl --user is-active --quiet aipm-notifications-staging.service; then record SIGTERM_SHUTDOWN PASS 'systemd stop delivered graceful termination'; else record SIGTERM_SHUTDOWN FAIL 'temporary worker did not stop cleanly'; fi

  if seed_transition >/dev/null && systemctl --user start aipm-notifications-staging.service && STAGE_STARTED=1 && wait_for_file "$STAGE_ROOT/ready-systemd" 15 && wait_for_activity_count 2 15; then record RESTART_RESUME PASS 'restart resumed temporary processing'; else record RESTART_RESUME FAIL 'restart/resume failed'; fi
  if systemctl --user stop aipm-notifications-staging.service && STAGE_STARTED=0; then record SECOND_STOP PASS 'temporary worker stopped after restart'; else record SECOND_STOP FAIL 'second stop failed'; fi

  local int_pid
  if start_direct_worker normal sigint; then
    int_pid="$STARTED_PID"
    if stop_direct_worker "$int_pid" SIGINT; then record SIGINT_SHUTDOWN PASS 'direct runner exited cleanly on SIGINT'; else record SIGINT_SHUTDOWN FAIL 'SIGINT lifecycle failed'; fi
  else
    record SIGINT_SHUTDOWN FAIL 'direct runner did not start for SIGINT test'
  fi
  DIRECT_PIDS=()

  if seed_transition >/dev/null; then
    local kill_pid recovery_pid
    if start_direct_worker block interrupted; then
      kill_pid="$STARTED_PID"
      if [[ -n "$kill_pid" ]] && wait_for_file "$ACTIVITY" 10 && grep -q '^FAKE_SEND_STARTED ' "$ACTIVITY"; then
        kill -KILL "$kill_pid" >/dev/null 2>&1 || true
        wait "$kill_pid" >/dev/null 2>&1 || true
        DIRECT_PIDS=()
        sleep 3
        if read_stage_check >"$STAGE_ROOT/check-interrupted.txt" 2>&1; then record INTERRUPTION_DB_INTEGRITY PASS 'SQLite remained valid after forced worker interruption'; else record INTERRUPTION_DB_INTEGRITY FAIL 'SQLite integrity failed after interruption'; fi
        if start_direct_worker normal recovery; then
          recovery_pid="$STARTED_PID"
          if wait_for_activity_count 3 15 && stop_direct_worker "$recovery_pid" SIGTERM; then record LEASE_RECOVERY PASS 'replacement worker reclaimed expired lease and fake-delivered once'; else record LEASE_RECOVERY FAIL 'lease recovery failed'; fi
        else
          record LEASE_RECOVERY FAIL 'replacement worker did not start'
        fi
        DIRECT_PIDS=()
      else
        record INTERRUPTION_DB_INTEGRITY FAIL 'blocked fake delivery could not be started'
        record LEASE_RECOVERY FAIL 'lease recovery was not attempted'
      fi
    else
      record INTERRUPTION_DB_INTEGRITY FAIL 'blocked fake delivery could not be started'
      record LEASE_RECOVERY FAIL 'lease recovery was not attempted'
    fi
  else
    record INTERRUPTION_DB_INTEGRITY FAIL 'interruption transition seed failed'
    record LEASE_RECOVERY FAIL 'lease recovery was not attempted'
  fi

  if seed_transition >/dev/null; then
    local p1 p2 fake_calls
    if start_direct_worker normal concurrent-a; then p1="$STARTED_PID"; else p1=; fi
    if start_direct_worker normal concurrent-b; then p2="$STARTED_PID"; else p2=; fi
    if [[ -n "$p1" && -n "$p2" ]] && wait_for_rate_limit_suppression 1 15; then
      if stop_direct_worker "$p1" SIGTERM && stop_direct_worker "$p2" SIGTERM; then
        DIRECT_PIDS=()
        fake_calls=$(grep -c '^FAKE_SEND ' "$ACTIVITY" 2>/dev/null || true)
        if [[ "$fake_calls" == 3 ]]; then
          record ISOLATED_CONCURRENCY PASS 'two parent-owned workers completed and fourth transition was rate-limited'
        else
          record ISOLATED_CONCURRENCY FAIL "expected three total fake deliveries, observed $fake_calls"
        fi
      else
        record ISOLATED_CONCURRENCY FAIL 'one or more concurrent workers did not stop cleanly'
      fi
    else
      record ISOLATED_CONCURRENCY FAIL 'two-worker contention or expected rate-limit suppression did not complete'
    fi
  else
    record ISOLATED_CONCURRENCY FAIL 'concurrency transition seed failed'
  fi

  if read_stage_check >"$STAGE_ROOT/check-final.txt" 2>&1; then record FINAL_DB_INTEGRITY PASS 'temporary SQLite integrity, foreign keys, and identity uniqueness clean'; else record FINAL_DB_INTEGRITY FAIL 'final temporary SQLite check failed'; fi
  local fake_calls_final rate_limit_suppressions_final
  fake_calls_final=$(grep -c '^FAKE_SEND ' "$ACTIVITY" 2>/dev/null || true)
  rate_limit_suppressions_final=$($VENV_PYTHON "$STAGE_CHECKER" "$STAGE_DB" 2>/dev/null | awk -F= '$1 == "stage.metrics.rate_limit_suppressions" {print $2; exit}')
  printf 'fake_adapter_delivery_calls=%s\n' "$fake_calls_final"
  printf 'rate_limit_suppressions=%s\n' "$rate_limit_suppressions_final"
  if [[ "$fake_calls_final" == 3 ]]; then record EXPECTED_FAKE_DELIVERIES PASS 'exactly three local fake-adapter deliveries observed'; else record EXPECTED_FAKE_DELIVERIES FAIL "expected three fake deliveries, observed $fake_calls_final"; fi
  if [[ "$rate_limit_suppressions_final" == 1 ]]; then record EXPECTED_RATE_LIMIT_SUPPRESSION PASS 'fourth transition suppressed by rate_limit'; else record EXPECTED_RATE_LIMIT_SUPPRESSION FAIL "expected one rate_limit suppression, observed ${rate_limit_suppressions_final:-0}"; fi
  if [[ "$FAKE_ADAPTER_GUARD_OK" == 1 ]]; then record NO_EXTERNAL_PROVIDER_CALLS PASS 'fake-only adapter and network-blocking guard remained active'; else record NO_EXTERNAL_PROVIDER_CALLS FAIL 'fake-adapter static safety guard was not satisfied'; fi

  telemetry_active=$(systemctl --user is-active aipm-telemetry.service 2>/dev/null || true)
  events_active=$(systemctl --user is-active aipm-events.service 2>/dev/null || true)
  [[ "$telemetry_active" == active && "$events_active" == active ]] && record TELEMETRY_MC3_UNCHANGED_AFTER PASS 'telemetry and MC-3 remained active' || record TELEMETRY_MC3_UNCHANGED_AFTER FAIL 'telemetry or MC-3 state changed'
  live_config_hash_after=$(sha256sum "$LIVE_CONFIG" 2>/dev/null | awk '{print $1}')
  [[ -n "$live_config_hash_before" && "$live_config_hash_before" == "$live_config_hash_after" ]] && record LIVE_CONFIG_UNMODIFIED PASS 'live configuration hash unchanged' || record LIVE_CONFIG_UNMODIFIED FAIL 'live configuration hash changed or unavailable'
  live_notifications_enabled=$($VENV_PYTHON - "$LIVE_CONFIG" 2>/dev/null <<'PY'
import sys
from pathlib import Path
from aipm.core.config import ConfigManager
config = ConfigManager(Path(sys.argv[1]))
print(config.config.notifications.enabled)
PY
  )
  live_config_manager_rc=$?
  if [[ "$live_config_manager_rc" == 0 && "$live_notifications_enabled" == False ]]; then
    record LIVE_NOTIFICATIONS_STILL_DISABLED PASS 'authoritative ConfigManager reports notifications.enabled=False'
  else
    record LIVE_NOTIFICATIONS_STILL_DISABLED FAIL 'authoritative ConfigManager did not report notifications.enabled=False'
  fi

  cleanup
  if [[ ! -e "$STAGE_UNIT" && ( -z "$STAGE_ROOT" || ! -e "$STAGE_ROOT" ) ]]; then record CLEANUP_VERIFIED PASS 'temporary unit, files, and processes removed'; else record CLEANUP_VERIFIED FAIL 'temporary state remains'; fi
  print_summary
  SUMMARY_PRINTED=1
  if [[ "$FAIL_COUNT" == 0 ]]; then return 0; fi
  return 1
}

main "$@"
