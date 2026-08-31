# MC-6.12 Release and Cutover Runbook

## Overview

This document defines the exact procedure for deploying the MC-6.12 control plane to the VPS and performing the first real external mutation.

## BEFORE CUTOVER

1. Create a release commit on the `main` branch (clean tree, all MC-6.12 files).
2. Run `python ops/validate-release.py` → must pass.
3. Run `python ops/build-release.py` → generates `release-manifest.json`.
4. Record the commit SHA and `artifact_sha256` from the manifest.
5. Verify the VPS is backed up (systemd units, DB, configuration).
6. Confirm the operator has root terminal access.

## CHECKPOINTS

### CHECKPOINT 0: Repository/version verification
```
git status --short  # must be clean
git rev-parse HEAD  # must match the release commit
python ops/validate-release.py  # must pass
```

### CHECKPOINT 1: Identity creation
```
sudo bash ops/setup-aipm-identity.sh
id aipm
id aipm-executor
id -Gn aipm  # must include aipm-executor group
id -Gn aipm-executor  # must NOT include sudo or docker
```
**Stop if**: users missing, privileged groups present, nologin shell missing.

### CHECKPOINT 2: Filesystem preparation
```
ls -la /var/lib/aipm/state/telemetry/
ls -la /var/lib/aipm/logs/
# DB must be aipm:aipm 600
# Logs must be aipm:aipm 750
```
**Stop if**: wrong ownership, wrong mode, DB corruption.

### CHECKPOINT 3: DB migration
```
sqlite3 /var/lib/aipm/state/telemetry/mission_control.db "PRAGMA integrity_check;"
sqlite3 /var/lib/aipm/state/telemetry/mission_control.db "SELECT COUNT(*) FROM project_plans;"
```
**Stop if**: integrity_check fails, missing tables, wrong schema version.

### CHECKPOINT 4: Sudoers installation
```
sudo visudo -c
sudo -n -l -U aipm-executor
# Expected: (root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service
```
**Stop if**: visudo errors, broader rules detected, mina AIPM rule still present.

### CHECKPOINT 5: Unit installation
```
sudo cp ops/systemd/aipm-*.service /etc/systemd/system/
sudo systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/aipm-*.service
```
**Stop if**: verify fails, daemon-reload errors.

### CHECKPOINT 6: Executor socket validation
```
sudo systemctl start aipm-executor
ls -la /run/aipm/executor.sock
# Expected: srw-rw---- aipm-executor aipm-executor
```
**Stop if**: socket missing, world-writable, wrong owner.

### CHECKPOINT 7: Service startup
```
sudo systemctl start aipm-telemetry aipm-events aipm-dashboard
systemctl status aipm-telemetry aipm-events aipm-dashboard
# All must be active (running)
```
**Stop if**: any service failed.

### CHECKPOINT 8: Control-plane health
```
curl http://127.0.0.1:8787/healthz
# Expected: {"status":"ok"}
```
**Stop if**: unhealthy, wrong version.

### CHECKPOINT 9: Authorization-path validation
```
# Via transport: login, create plan, authorize, confirm
# Do NOT execute yet — verify the authorization path works end-to-end
```
**Stop if**: authorization fails, confirmation fails.

### CHECKPOINT 10: First real mutation
```
# Via transport: execute the confirmed action
# Monitor: audit chain, action state, executor receipt, verification
```
**Stop conditions**: see below.

## STOP CONDITIONS

| Condition | Action |
|---|---|
| Unexpected service user | STOP — investigate identity |
| Unexpected sudo privilege | STOP — privilege drift |
| Unexpected socket ownership | STOP — IPC security |
| Executor unavailable | STOP — no blind retry |
| Executor reports unknown | STOP — reconcile |
| Duplicate receipt | STOP — integrity failure |
| Verification mismatch | STOP — investigate |
| Unexpected DB state | STOP — corruption |
| Production target appears | STOP — scope violation |

## ROLLBACK

Per-checkpoint rollback (targeted, not destructive):
- Identity: `userdel` only after verifying no owned files
- Filesystem: targeted `rm` on specific paths
- DB: restore from `.pre-migration.bak`
- Sudoers: remove `/etc/sudoers.d/aipm-*` only
- Units: restore backed-up units
- Services: `systemctl stop` + restore old units

## POST-CUTOVER

```
curl /health → build.commit_sha matches release
systemctl show aipm-telemetry --property=User → User=aipm
systemctl show aipm-executor --property=User → User=aipm-executor
ls -la /run/aipm/executor.sock → mode 0660, owner aipm-executor
sudo -n -l -U aipm-executor → only exact systemctl restart rule
```

## FIRST MUTATION

1. Login to the transport (or use the CLI)
2. Authorize a ProjectPlan mutation
3. Confirm the action
4. Capture snapshot
5. Execute (routes through gate → IPC → executor → sudo → systemctl)
6. Verify: audit chain, action state, executor receipt, independent verification
7. Confirm: VERIFIED_SUCCESS, plan mutated, lease released
