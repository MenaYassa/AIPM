# MC-6.12 Release and Cutover Runbook

## Overview

This document defines the exact procedure for deploying the MC-6.12 control plane to the VPS and performing the first real external mutation.

## BEFORE CUTOVER

The authoritative release artifact is **built BEFORE deployment** from the exact
release commit, on the build host (not on the VPS). The VPS only **verifies** the
supplied artifact identity — it never regenerates the authoritative release manifest.

1. Create a release commit on the `main` branch (clean tree, all MC-6.12 files).
2. Run `python ops/validate-release.py --development` → must pass.
3. From the exact release commit (`RELEASE_COMMIT`), generate the authoritative
   release defaults with `python ops/build-release.py` → produces the file listing,
   per-file SHA-256, and `artifact_sha256`.
4. Build the release artifact: copy the manifest-listed deployable files plus the
   generated `build_meta.json` (carrying `commit_sha = RELEASE_COMMIT` and
   `version = mc612-v1`) into a clean directory. Do NOT reconstruct the artifact on
   the VPS; do NOT build from a dirty tree.
5. Record: `RELEASE_COMMIT`, `RELEASE_VERSION=mc612-v1`, `ARTIFACT_SHA256`.
6. The VPS receives the artifact and its manifest, verifies every file SHA-256 and
   the `artifact_sha256`, and resolves build identity from the supplied
   `build_meta.json` (which identifies the same commit). VPS-side re-generation of
   build metadata is a **deployment verification manifest** only, never the
   authoritative release identity.
7. Verify the VPS is backed up (systemd units, DB, configuration).
8. Confirm the operator has root terminal access.

## CHECKPOINTS

### CHECKPOINT 0: Repository/version verification
```
git status --short  # must be clean
git rev-parse HEAD  # must equal RELEASE_COMMIT
git ls-remote origin refs/heads/main  # must equal RELEASE_COMMIT
python ops/validate-release.py --development  # must pass
```

The artifact deployed in this cutover was built before deployment from
`RELEASE_COMMIT`. Its authoritative `ARTIFACT_SHA256` is recorded in the build-time
`release-manifest.json` generated from that exact commit. Verify the staged
artifact's files and manifest against that generated hash before proceeding.

### CHECKPOINT 1: Identity creation
```
sudo bash ops/setup-aipm-identity.sh --dry-run   # review every mutating command first
sudo bash ops/setup-aipm-identity.sh --apply
id aipm
id aipm-executor
id -Gn aipm            # must include aipm-executor and aipm-runtime
id -Gn aipm-executor   # must include aipm-runtime; must NOT include aipm, sudo, or docker
getent group aipm-runtime  # members must be exactly aipm and aipm-executor
```
The script is staged (PRECHECK → BACKUP → IDENTITY → RUNTIME_DIRS →
PERMISSIONS → SUDOERS → VERIFY). Each stage reports its exact name on failure and
STOPS — there is no automatic rollback, no database migration, and no service
restart inside this script. `--print-rollback` shows targeted rollback procedures.

**Stop if**: users missing, privileged groups present, nologin shell missing,
aipm-runtime membership is not exactly {aipm, aipm-executor}.

### CHECKPOINT 2: Filesystem preparation
```
ls -la /var/lib/aipm/state/telemetry/
ls -la /var/lib/aipm/logs/
# DB must be aipm:aipm 600
# Logs must be aipm:aipm 750
# App code: group aipm-runtime, dirs 0750, files 0640; owner unchanged
```
**Stop if**: wrong ownership, wrong mode, DB corruption.

### CHECKPOINT 3: DB migration (separate explicit script)
```
sudo bash ops/migrate-aipm-state.sh --dry-run
sudo bash ops/migrate-aipm-state.sh --apply
sudo sqlite3 /var/lib/aipm/state/telemetry/mission_control.db "PRAGMA integrity_check;"
```
The migration script (PRECHECK → QUIESCE_CHECK → SOURCE_VERIFY → BACKUP →
COPY → DEST_VERIFY → SUMMARY) refuses to run while a writer holds the source DB
open (`fuser` gate → explicit operator checkpoint to stop aipm-telemetry), takes a
timestamped pre-migration backup that is never pruned or overwritten, copies only
from the verified backup (no stale `-wal`/`-shm` at the destination), and verifies
integrity, schema identity, per-table row counts, and exact owner/mode at the
destination. The source DB is never modified or removed. Rollback material is
listed by `--print-rollback`.

**Stop if**: integrity_check fails, missing tables, row-count mismatch, wrong
owner/mode, or a writer was active at QUIESCE_CHECK.

### CHECKPOINT 4: Sudoers installation
```
sudo visudo -c
sudo -n -l -U aipm-executor
# Expected: (root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service
# Installed by CHECKPOINT 1 transactionally: candidate -> visudo -cf -> backup
# of the prior rule -> atomic mv; on validator failure only the candidate is
# removed and the previous rule remains untouched.
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

Per-checkpoint rollback (targeted, not destructive; `--print-rollback` on each
cutover script prints the exact current procedures):
- Identity: `userdel` only after verifying no owned files
- Filesystem: targeted `rm`/`chown` on specific paths
- DB: restore from the newest `/var/lib/aipm/state/telemetry/backups/mission_control.db.pre-migration.*.bak` (backups are never pruned by the script)
- Sudoers: restore the exact prior rule from `/etc/sudoers.d/.aipm-backup/`, then `visudo -c`
- Units: restore backed-up units
- Services: `systemctl stop` + restore old units

## POST-CUTOVER

```
curl /health → build.commit_sha == RELEASE_COMMIT, build.version == mc612-v1, build.environment == production
systemctl show aipm-telemetry --property=User → User=aipm
systemctl show aipm-executor --property=User → User=aipm-executor
ls -la /run/aipm/executor.sock → mode 0660, owner aipm-executor
sudo -n -l -U aipm-executor → only exact systemctl restart rule
```

The `/health` `build` object is resolved from the deployed artifact's `build_meta.json`
(no `.git` dependency). A mismatch between the deployed commit and `RELEASE_COMMIT`
means the wrong artifact was deployed — STOP and investigate. Any VPS-side manifest
used for verification is a **deployment verification manifest** and does not become
the authoritative release identity.

## FIRST MUTATION

1. Login to the transport (or use the CLI)
2. Authorize a ProjectPlan mutation
3. Confirm the action
4. Capture snapshot
5. Execute (routes through gate → IPC → executor → sudo → systemctl)
6. Verify: audit chain, action state, executor receipt, independent verification
7. Confirm: VERIFIED_SUCCESS, plan mutated, lease released
