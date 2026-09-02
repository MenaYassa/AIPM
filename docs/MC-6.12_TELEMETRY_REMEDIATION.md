# MC-6.12 Telemetry & Service-Runtime Remediation

> **Scope note — 2026-09-01.** This document is the authoritative operations, deployment, rollback, and diagnostics record for the MC-6.12 telemetry/service-runtime remediation (privilege-boundary service runtime scopes, project telemetry refresh, dashboard freshness, Git enrichment hardening). It covers the read-only telemetry plane only. The separate MC-6.12 executor/action plane remains blocked and is documented by [`MC-6.12_PRODUCTION_ARCHITECTURE.md`](MC-6.12_PRODUCTION_ARCHITECTURE.md), [`MC-6.12_PRIVILEGE_BOUNDARY.md`](MC-6.12_PRIVILEGE_BOUNDARY.md), and [`MC-6.12_RELEASE_AND_CUTOVER.md`](MC-6.12_RELEASE_AND_CUTOVER.md). Overall repository status is reconciled by [`CURRENT_STATUS.md`](CURRENT_STATUS.md).

## Purpose

This document describes the system **as it actually exists** after the MC-6.12 telemetry remediation:

- why project telemetry and dashboard freshness previously failed,
- what was changed and why,
- how to deploy and roll back the change,
- how to diagnose every known failure mode without repeating the original investigation,
- which residual limitations remain on the host and why they are accepted.

Nothing in this remediation authorizes mutations. Mission Control remains a read-only observer of the VPS.

## Root-cause chain

The remediation addressed a chain of independent defects that compounded into "projects unavailable / dashboard freshness frozen":

1. **`aipm-telemetry.service` lacked `AIPM_CONFIG`.** The telemetry daemon therefore loaded the wrong/default configuration (the process-user config path) and discovered no configured projects. With no project configuration, project sampling produced nothing to persist.
2. **Git enrichment ran as `aipm` against repositories owned by `mina`.** Git rejects repositories whose owner differs from the accessing user unless an explicit `safe.directory` exception exists.
3. **The `aipm` repository's Git metadata is not fully readable by the telemetry daemon.** `.git/refs/heads/main` is (or has been) `mina:mina` with restrictive permissions. `safe.directory` only clears Git's dubious-ownership check; it does **not** grant filesystem read permission. This is a host-permissions residual, not a code defect — see [Known host residual](#known-host-residual).
4. **One Git enrichment failure previously failed the entire project discovery operation.** MC-6.12 isolates Git enrichment failures per project so a single unreadable or dubious repository cannot destroy the inventory of the remaining projects.
5. **The dashboard previously did not continuously re-read persisted project telemetry.** Project freshness could remain frozen until a dashboard restart. MC-6.12 hydrates persisted project samples at startup and periodically re-hydrates the dashboard cache from the telemetry database.
6. **Network telemetry required `AF_NETLINK`** under the current systemd sandboxing; without it, network-dependent measurements were unavailable.
7. **Dashboard journald access required journal permissions.** The `aipm` service account was added to `systemd-journal` as a host-provisioning step.
8. **Dashboard log-source configuration needed to respect `AIPM_LOG_FILE`** so application-file logs could be read from the configured location rather than only the repository-relative default.
9. **Journald parsing needed a longer bounded timeout and Python logging timestamp support.**
10. **MC-3 service-health semantics** contained a latest-event ordering issue and previously conflated event age with pipeline liveness.

## Implemented behavior (MC-6.12)

The following is the implemented, verified behavior. It is derived from the working-tree diff at `ee308ac600f2148166efa1146a455b6bc1fe2a06` plus live validation.

### Service environment

Both long-running services (`aipm-telemetry.service`, `aipm-dashboard.service`) receive:

```text
AIPM_CONFIG=/home/ubuntu/aipm/config/aipm.yaml
AIPM_TELEMETRY_DB=/var/lib/aipm/state/telemetry/mission_control.db
AIPM_LOG_FILE=/var/lib/aipm/logs/aipm.log
```

`ExecStart` runs `/home/ubuntu/aipm/.venv/bin/aipm telemetry run` (telemetry) and `/home/ubuntu/aipm/.venv/bin/aipm dashboard --host 127.0.0.1 --port 8787` (dashboard) from `WorkingDirectory=/home/ubuntu/aipm`.

### Project discovery and Git enrichment

- Project discovery remains authoritative through `ProjectService.discover()`. There is **no** second independent project discovery mechanism; the dashboard consumes what telemetry persisted.
- Git commands use per-invocation configuration: `git -c safe.directory=<path> <args>` (`src/aipm/providers/git/provider.py`). This scopes the exception to the exact repository being enriched and never persists Git configuration.
- Git discovery remains bounded and read-only: fixed read-only commands, `shell=False`, `GIT_OPTIONAL_LOCKS=0`, a new process group, a hard deadline, cooperative cancellation, and capped output. Telemetry never runs Git state-changing operations.
- Individual Git enrichment failures do not destroy the project discovery result. `ProjectService.discover()` catches per-project `GitError` (re-raising `GitDiscoveryCancelled` first, since it is a subclass) and logs `project:git_enrichment_skipped - Git telemetry unavailable for <project>: <reason>`.
- Unreadable Git metadata degrades gracefully rather than crashing discovery: `rev-parse HEAD`, `symbolic-ref`, `log -1`, and `stash list` failures produce a partial project snapshot (`branch` null, ahead/behind unavailable) while other fields (including `dirty` from a successful `git status`) remain valid.

### Telemetry persistence and dashboard freshness

- Telemetry persists project samples into the existing `project_samples` table. **No database schema migration was introduced.**
- Dashboard startup hydrates project state (and resource state) from persisted telemetry through a read-only repository connection.
- The dashboard periodically refreshes project history from the telemetry DB. The refresh is throttled with a monotonic clock at `telemetry.project_interval_seconds` (60 seconds by default) and is evaluated on each `/api/overview` request. A refresh failure is logged (`Project telemetry history refresh unavailable`) and never fails the overview response.
- The dashboard remains a read-only consumer of telemetry state: its SQLite connections open with `read_only=True` and `PRAGMA query_only = ON`, and the unit binds the telemetry state directory via `BindReadOnlyPaths`.
- Project freshness is based on persisted telemetry timestamps and degrades to `stale` (or `never_sampled`) when telemetry stops. It never reports fresh on frozen data.

### Logging and network telemetry

- `AF_NETLINK` is allowed in the relevant systemd services (required for host network telemetry under sandboxing).
- Docker access belongs to telemetry (`SupplementaryGroups=docker` on the telemetry unit only), not to the dashboard.
- Journald log access is bounded (10-second command timeout, capped output).
- Application-file logs are read through the configured `AIPM_LOG_FILE` environment override in the logs capability.
- Journald parsing supports Python logging timestamps (`YYYY-MM-DD HH:MM:SS,mmm`) in addition to ISO timestamps, and uses a longer bounded timeout.

### MC-3 service health semantics

- The service-health view selects the latest event by `occurred_at` (falling back to `created_at`) with a bounded query (`limit=5000`), instead of relying on first-event ordering.
- Event age is no longer conflated with pipeline liveness: query success proves the event pipeline is live; event age alone does not mark the pipeline unavailable.

## Configuration and current runtime result

The documented production configuration (`config/aipm.yaml`) contains these project search paths:

```text
/home/ubuntu/aipm
/home/ubuntu/fastsdcpu
/home/ubuntu/invoicing
/home/ubuntu/local-ai-packaged
/home/ubuntu/EAG
```

The runtime currently discovers **four** projects — `aipm`, `invoicing`, `EAG`, `local-ai-packaged` — because `/home/ubuntu/fastsdcpu` does not currently produce a discovered project (no recognizable project under that root at validation time).

> These names are **not hard-coded project identities**. They are the current runtime result of the configured search paths. Changing the search paths changes the discovered inventory; a project that later appears under an existing root is discovered automatically on the next project sampling cycle.

## Architecture

```text
Host telemetry (CPU, memory, disk, load, uptime, network)
        ↓
Telemetry sampler (aipm-telemetry.service, coordinator + scheduler)
        ↓
Project discovery (ProjectService.discover) / Docker & resource sampling
        ↓
SQLite telemetry history (/var/lib/aipm/state/telemetry/mission_control.db)
        ↓
Dashboard hydration/cache (read-only DB reads, startup + throttled refresh)
        ↓
Dashboard API/UI (aipm-dashboard.service, GET-only routes)
```

Distinct telemetry layers — do not conflate them when diagnosing:

| Layer | Producer | Cadence | Consumer |
|---|---|---|---|
| Live/fast telemetry | Dashboard in-process snapshot (host, Docker presence, tunnel) | per `/api/overview` request | dashboard response |
| Slow project sampling | Telemetry sampler project slot (`project_interval_seconds`, single-flight) | 60 s default | `project_samples` table |
| Persisted telemetry | Telemetry sampler → SQLite history | every sample | authoritative history |
| Dashboard cache | Hydrated from persisted telemetry | startup + throttled refresh | overview response |
| Dashboard freshness | Derived from persisted timestamps (`stale_after_seconds` = 180 s default) | per response | freshness status |

The fast dashboard loop never blocks on slow project work: project discovery and Git enrichment run in the telemetry sampler under cooperative cancellation, single-flight slots, and bounded deadlines. The dashboard only re-reads persisted results.

## Systemd security model

Both units run `User=aipm`, `Group=aipm`, `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `RestrictSUIDSGID=true`, `RestrictNamespaces=true`, `LockPersonality=true`, `ProtectKernelTunables/Modules/ControlGroups=true`, and `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK` (the netlink family is required for network telemetry; removing it degrades network observation).

**Telemetry** (`ops/systemd/aipm-telemetry.service`) is the sampling plane:

- `SupplementaryGroups=docker` — the only unit with Docker access, used for container observation.
- `ReadWritePaths=/var/lib/aipm/state/telemetry /var/lib/aipm/logs /var/lib/aipm/.config` — the only writable paths; it gains no unrelated write access.
- `ProtectHome=read-only` — repositories are read-observed, never modified.
- It retains all kernel/cgroup restrictions listed above and never invokes sudo.

**Dashboard** (`ops/systemd/aipm-dashboard.service`) is the read-only presentation plane:

- No Docker group access — it cannot observe or touch Docker directly.
- `BindReadOnlyPaths=/var/lib/aipm/state/telemetry` — telemetry state is bind-mounted read-only into its namespace.
- `ReadWritePaths=/var/lib/aipm/logs /var/lib/aipm/.config /var/lib/aipm/.local` — narrowly scoped to its own logs/config/state.
- It retains `NoNewPrivileges` and all system-protection flags; `ProtectHome=false` is required so configured project search paths remain visible for path validation, with all mutation still impossible.

> The documented isolation is exactly what the unit files provide. Do not claim stronger isolation in downstream documents, and do not weaken flags to "fix" symptoms — diagnose through the [troubleshooting section](#troubleshooting) instead.

## Known host residual

> **This residual is expected behavior, not a defect in MC-6.12. Repair is a deliberate host-provisioning decision, not part of deployment.**

The `aipm` repository has Git metadata whose ownership/permissions may prevent complete Git enrichment when accessed by the `aipm` service account. In particular, `.git/refs/heads/main` is (or has been) owned `mina:mina` with restrictive permissions, so the daemon cannot read the ref files.

- `safe.directory` solves Git's ownership/dubious-repository check. It does **not** grant filesystem permissions.
- Therefore, for the affected repository: `branch` may legitimately be null; `ahead`/`behind` may be unavailable; other project fields remain valid; `dirty` may still be known from a successful `git status`; and project discovery itself continues to succeed.
- Other repositories discovered from the same search paths are unaffected by this residual.

**Operational caveat:** if repositories are routinely modified by `mina`, restrictive umask/ownership can recreate this problem after any permissions repair. Any repair must therefore be paired with provisioning changes (umask/ownership policy for new Git metadata), or it will regress. Do not change repository or host permissions as part of MC-6.12; if a repair is ever chosen, do it explicitly as host provisioning with the affected services stopped or restarted afterward, and re-verify enrichment.

## Deployment

Deploy from a known repository state on the VPS. There are **no** schema migrations and **no** destructive steps. All commands below are safe and non-destructive; restarts affect only the two Mission Control services.

### Prerequisites

1. Repository checked out at `/home/ubuntu/aipm` with the MC-6.12 telemetry patch present (working tree contains the remediation; see [Rollback](#rollback) for the exact file set).
2. `aipm` service account exists and `aipm` is a member of `docker` (telemetry only) and `systemd-journal` (host-provisioning prerequisite):

   ```bash
   getent passwd aipm && id aipm
   getent group docker | grep -w aipm
   getent group systemd-journal | grep -w aipm
   ```

   Membership changes are host-provisioning actions; if they are missing, resolve them deliberately before deploying rather than auto-fixing from this runbook.
3. State/log directories exist and are writable by `aipm`: `/var/lib/aipm/state/telemetry`, `/var/lib/aipm/logs`.
4. Configuration exists at `/home/ubuntu/aipm/config/aipm.yaml` with the expected search paths.

### Procedure

1. **Verify repository state** (record it before touching services):

   ```bash
   cd /home/ubuntu/aipm && git rev-parse HEAD && git status --short
   ```

2. **Install the updated units** (verify the actual unit paths — the repo sources are `ops/systemd/`, installed copies live in `/etc/systemd/system/`):

   ```bash
   sudo install -m 0644 /home/ubuntu/aipm/ops/systemd/aipm-telemetry.service /etc/systemd/system/aipm-telemetry.service
   sudo install -m 0644 /home/ubuntu/aipm/ops/systemd/aipm-dashboard.service /etc/systemd/system/aipm-dashboard.service
   ```

3. **Reload the systemd manager:**

   ```bash
   sudo systemctl daemon-reload
   ```

4. **Restart telemetry first** (it is the producer; the dashboard unit orders `After=aipm-telemetry.service`):

   ```bash
   sudo systemctl restart aipm-telemetry.service
   ```

5. **Allow telemetry to produce samples.** Wait at least one project sampling interval (60 s by default) before restarting the dashboard, so persisted project samples exist for startup hydration:

   ```bash
   sleep 75
   ```

6. **Restart the dashboard:**

   ```bash
   sudo systemctl restart aipm-dashboard.service
   ```

7. **Verify both services:**

   ```bash
   systemctl is-active aipm-telemetry.service aipm-dashboard.service
   systemctl status aipm-telemetry.service aipm-dashboard.service --no-pager -n 5
   ```

   Expected: both `active (running)`.

8. **Verify project freshness** (freshness must advance with the dashboard still running — this proves the periodic refresh):

   ```bash
   curl -s http://127.0.0.1:8787/api/overview | jq '.projects.freshness'
   sleep 65
   curl -s http://127.0.0.1:8787/api/overview | jq '.projects.freshness'
   ```

   Expected: `status: "fresh"` and `sampled_at` advancing between the two calls with no dashboard restart (check `systemctl show -p ActiveEnterTimestamp aipm-dashboard.service`).

9. **Verify project count:**

   ```bash
   curl -s http://127.0.0.1:8787/api/overview | jq '.projects.available, (.projects.projects | length)'
   ```

   Expected: `1` (projects available) and the number of currently discovered projects (4 at validation time — see [Configuration and current runtime result](#configuration-and-current-runtime-result)).

10. **Verify network telemetry:**

    ```bash
    curl -s http://127.0.0.1:8787/api/overview | jq '.host.network'
    ```

    Expected: `available: true` with connection/interface counts, not an `"unavailable"`/error payload (see diagnostics case I if it is).

11. **Verify logs:**

    ```bash
    curl -s "http://127.0.0.1:8787/api/logs?range=24h&limit=50" | jq '.returned_lines'
    sudo journalctl -u aipm-dashboard.service -n 20 --no-pager
    ```

    Expected: log lines returned from both the application log file and journald sources.

12. **Verify health and telemetry DB timestamps:**

    ```bash
    curl -s http://127.0.0.1:8787/healthz
    sqlite3 "file:/var/lib/aipm/state/telemetry/mission_control.db?mode=ro" \
      "SELECT MAX(sampled_at) FROM project_samples;" && date
    ```

    Expected: `{"status":"ok"}` and the latest `project_samples` timestamp within roughly the last project interval when compared against the **host clock** (`date`), not an assumed UTC clock — see the timestamp note in diagnostics case N.

## Rollback

Rollback restores the pre-remediation behavior. **No database rollback or migration is necessary** — the MC-6.12 telemetry patch introduced no schema changes, and old code reads the same `project_samples` table.

1. **Restore the previous code state** (discard the MC-6.12 telemetry patch from the working tree). The remediation patch consists of these modified files:

   ```text
   ops/systemd/aipm-telemetry.service
   ops/systemd/aipm-dashboard.service
   src/aipm/providers/git/provider.py
   src/aipm/providers/logs.py
   src/aipm/repositories/telemetry/base.py
   src/aipm/repositories/telemetry/sqlite.py
   src/aipm/services/project/service.py
   src/aipm/services/telemetry/project.py
   src/aipm/capabilities/dashboard/api.py
   src/aipm/capabilities/dashboard/logs_api.py
   src/aipm/capabilities/dashboard/service_health_api.py
   tests/test_mc68_logs.py
   tests/test_service_health_api.py
   ```

   plus the new (untracked) test files `tests/test_mc612_dashboard_refresh.py`, `tests/test_mc612_git_safe_directory.py`, `tests/test_mc612_log_parsing.py`, `tests/test_mc612_projects.py`, `tests/test_mc612_units.py`.

   ```bash
   cd /home/ubuntu/aipm
   git checkout -- ops/systemd/aipm-telemetry.service ops/systemd/aipm-dashboard.service \
     src/aipm/providers/git/provider.py src/aipm/providers/logs.py \
     src/aipm/repositories/telemetry/base.py src/aipm/repositories/telemetry/sqlite.py \
     src/aipm/services/project/service.py src/aipm/services/telemetry/project.py \
     src/aipm/capabilities/dashboard/api.py src/aipm/capabilities/dashboard/logs_api.py \
     src/aipm/capabilities/dashboard/service_health_api.py \
     tests/test_mc68_logs.py tests/test_service_health_api.py
   ```

   > Note: `ops/migrate-aipm-state.sh`, `ops/setup-aipm-identity.sh`, and `ops/staging/mc5-gate2-staging.sh` show mode-only changes that are pre-existing unrelated working-tree state — they are not part of this remediation and must not be reverted as part of telemetry rollback.

2. **Restore the previous systemd unit versions** (the repository's HEAD versions are the pre-remediation units; after step 1 they are already restored in the tree):

   ```bash
   sudo install -m 0644 /home/ubuntu/aipm/ops/systemd/aipm-telemetry.service /etc/systemd/system/aipm-telemetry.service
   sudo install -m 0644 /home/ubuntu/aipm/ops/systemd/aipm-dashboard.service /etc/systemd/system/aipm-dashboard.service
   ```

3. **Reload and restart:**

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart aipm-telemetry.service
   sudo systemctl restart aipm-dashboard.service
   ```

4. **Verify rollback** with the same checks as deployment steps 7–12. Expected degraded-but-safe state after rollback: telemetry may again discover no configured projects (the original `AIPM_CONFIG` defect returns), and project freshness may freeze until a dashboard restart — that is the pre-remediation behavior, which is exactly what rollback should reproduce.

## Troubleshooting

All commands are read-only. `sqlite3` is always opened with `?mode=ro`. The dashboard is expected on `127.0.0.1:8787`. The telemetry database is `/var/lib/aipm/state/telemetry/mission_control.db`; the application log is `/var/lib/aipm/logs/aipm.log`.

| Case | Symptom | Likely cause | Verification (read-only) | Interpretation & safe action |
|---|---|---|---|---|
| A | Dashboard shows zero projects | Telemetry loaded wrong/default config (missing `AIPM_CONFIG`), or no search path yields projects | `systemctl show aipm-telemetry.service -p Environment` — expect `AIPM_CONFIG=/home/ubuntu/aipm/config/aipm.yaml` | If the variable is missing, the installed unit predates MC-6.12: redeploy units (deployment steps 2–4). If present, check `/api/overview` `.projects.search_paths` matches the configured roots and that at least one root contains a discoverable project. |
| B | Projects page lists projects but overview projects are empty | Dashboard cache not hydrated (dashboard predates patch, or refresh failing) | `curl -s http://127.0.0.1:8787/api/overview \| jq '.projects'`; `grep 'history refresh unavailable' /var/lib/aipm/logs/aipm.log \| tail` | Persisted samples exist but the dashboard cache never re-read them: confirm the dashboard unit/code includes the MC-6.12 refresher. Refresh failures are logged and never break the overview; the log line identifies the cause (e.g., DB unreadable). |
| C | Project freshness says `never_sampled` | Telemetry has never written a project sample (telemetry down since install, or no projects discovered) | `sqlite3 "file:/var/lib/aipm/state/telemetry/mission_control.db?mode=ro" "SELECT COUNT(*), MAX(sampled_at) FROM project_samples;"` | Zero rows → producer side is the problem (case A/N). Rows exist → the dashboard cache is the problem (case B). |
| D | Project freshness is `stale` | Telemetry stopped sampling; last persisted sample is older than `stale_after_seconds` (180 s) | Compare `.projects.freshness.sampled_at` with host clock; `systemctl is-active aipm-telemetry.service` | Freshness correctly degrading. Fix the producer (cases E/N); the dashboard recovers on its own — no dashboard restart required. |
| E | Telemetry DB stops advancing | Sampler stalled, deadlocked, or crash-looping | `sqlite3 "file:/var/lib/aipm/state/telemetry/mission_control.db?mode=ro" "SELECT MAX(sampled_at) FROM project_samples;"` twice, 20 s apart; `systemctl status aipm-telemetry.service --no-pager -n 30`; `sudo journalctl -u aipm-telemetry.service -n 50 --no-pager` | Timestamps frozen with the unit active → inspect the journal for repeated warnings (e.g., unbounded work, DB errors) and report the traceback; do not guess-fix. Unit down → `journalctl` for the exit reason, then restart once after fixing the cause. |
| F | Git says dubious ownership | Repository owned by another user than the `aipm` service account; per-invocation `safe.directory` missing (pre-remediation code) | `grep 'safe.directory' src/aipm/providers/git/provider.py` (expect the `-c safe.directory=<path>` argument); `grep 'dubious' /var/lib/aipm/logs/aipm.log \| tail` | With MC-6.12 code this error should not appear for configured roots. If it does, the running code predates the patch — redeploy (deployment steps 2–6). |
| G | Git `branch` is null for one project | Unreadable Git metadata (host-permissions residual, e.g. `mina:mina` ref files), even though `safe.directory` is applied | `sudo -u aipm git -C /home/ubuntu/aipm rev-parse HEAD` — expect failure if refs are unreadable; `ls -l /home/ubuntu/aipm/.git/refs/heads/` | Expected degradation per the [host residual](#known-host-residual): discovery and other fields remain valid. Repairing permissions is a deliberate host-provisioning decision — not an automatic fix — and may regress when `mina` writes to the repository again (umask caveat). |
| H | Git enrichment fails for one project | Any per-project Git failure (timeout, unreadable metadata, non-zero exit) | `grep 'git_enrichment_skipped' /var/lib/aipm/logs/aipm.log \| tail` | The warning names the project and reason. Discovery continues for other projects — this isolation is the intended MC-6.12 behavior. Address the project's own Git metadata access if complete enrichment is required. |
| I | Network says unavailable | `AF_NETLINK` missing from the unit (pre-remediation sandbox), or psutil network observation failing | `systemctl show aipm-telemetry.service aipm-dashboard.service -p RestrictAddressFamilies` — both must include `AF_NETLINK` | Missing family → redeploy updated units (steps 2–4, 6). Family present but still unavailable → check journal for the underlying exception; do not loosen other sandbox flags. |
| J | Logs say unavailable | Log source unconfigured/unreadable: `AIPM_LOG_FILE` not set, file missing, or journald source failing | `systemctl show aipm-dashboard.service -p Environment`; `ls -l /var/lib/aipm/logs/aipm.log`; `curl -s "http://127.0.0.1:8787/api/logs?range=24h&limit=50" \| jq '.returned_lines'` | Missing env → unit predates patch. File missing → telemetry may never have started writing (case N). Journald failure → case K. |
| K | Journald says permission denied | `aipm` service account not in `systemd-journal` group | `getent group systemd-journal \| grep -w aipm`; `sudo journalctl -u aipm-dashboard.service -n 10 --no-pager` (root works, confirming journald itself is healthy) | Membership is host provisioning: add deliberately (with service restart) as a provisioning decision, never auto-fix from the dashboard. |
| L | Health says stale/unavailable | Upstream telemetry source (Docker, network, events) degraded or frozen | `curl -s http://127.0.0.1:8787/healthz`; `curl -s http://127.0.0.1:8787/api/overview \| jq '.docker.resource_freshness, .projects.freshness'` | Health reflects bounded per-source degradation; identify which source is stale via the freshness fields, then fix that producer. The overview request itself must still succeed. |
| M | Dashboard appears healthy but data is old | Dashboard process up but cache not refreshing (pre-remediation behavior) or telemetry producer stopped | `systemctl show -p ActiveEnterTimestamp aipm-dashboard.service` (was it restarted recently to "fix" freshness?); then case E checks | Post-MC-6.12 the cache refreshes without restarts: if data only advances after a dashboard restart, the running code predates the patch — redeploy. If nothing advances even after restart, fix the producer (E/N). |
| N | Telemetry service running but `project_samples` not advancing | Wrong config loaded (no projects), discovery bounds rejecting the search paths, or a persistent Git/discovery failure | `sudo journalctl -u aipm-telemetry.service -n 50 --no-pager` (look for `project:snapshot`, `git_enrichment_skipped`, discovery warnings); `sqlite3 "file:/var/lib/aipm/state/telemetry/mission_control.db?mode=ro" "SELECT name, MAX(sampled_at) FROM project_samples GROUP BY name;"` | The journal shows whether discovery ran and what it skipped. Timestamp note: `sampled_at` is written from the service's clock in UTC representation; on this host (UTC+11) compare against the **host clock** (`date`), not an assumed UTC `now`, when judging freshness from raw SQL. |

## Test and validation state

Final validated state of the full suite:

```text
1144 passed
1 skipped
2 failed (pre-existing, unrelated to the MC-6.12 telemetry patch set)
```

**MC-6.12 focused tests pass; the full suite contains two known pre-existing failures unrelated to this change. Do not describe the suite as green.**

The two pre-existing failures (exact identities, reproduced from the repository):

- `tests/test_mc612_stage15_privilege.py::test_assert_detects_human_session_without_executor_rule`
- `tests/test_mc612_stage25a_identity_setup.py::test_s6_privileged_group_contamination_fails`

Both belong to the separate MC-6.12 privilege/identity workstream and fail identically at the committed checkpoint (`ee308ac600f2148166efa1146a455b6bc1fe2a06`): the stage15 assertion is unchanged in git history, and stage25a expects the setup script to report `aipm-executor is in a privileged group` while the script emits `aipm is in a privileged group` (a script-message/test-expectation drift outside this remediation's scope).

MC-6.12 focused tests (all pass): `tests/test_mc612_dashboard_refresh.py`, `tests/test_mc612_projects.py`, `tests/test_mc612_git_safe_directory.py`, `tests/test_mc612_units.py`, `tests/test_mc612_log_parsing.py`, `tests/test_mc68_logs.py`, `tests/test_service_health_api.py`.

## Design decisions

These decisions are deliberate and should not be "simplified" away by future changes:

- **One authoritative project discovery path.** `ProjectService.discover()` is the only discovery mechanism. The dashboard deliberately does not implement a second discovery path; it consumes persisted telemetry. Two discovery implementations would diverge and double host load.
- **Dashboard consumes persisted telemetry** for historical/project freshness rather than performing its own sampling. This keeps the dashboard's request path fast and read-only.
- **Per-project Git failure isolation.** One broken repository must never destroy the inventory of the remaining projects.
- **Per-invocation `safe.directory`** (`git -c safe.directory=<path>`) rather than persistent Git configuration: the exception is scoped to the exact repository being enriched, never written to any user's global Git config, and leaves the host's Git configuration untouched.
- **Graceful degradation for unreadable Git metadata.** Filesystem-permission failures produce partial snapshots (null branch, missing ahead/behind) instead of discovery failure — because `safe.directory` cannot and must not paper over host permissions.
- **Read-only dashboard telemetry access.** `read_only=True` connections, `PRAGMA query_only`, and `BindReadOnlyPaths` enforce the boundary in depth; the dashboard cannot initialize, migrate, or mutate telemetry state even if application code regressed.
- **Bounded project discovery.** Discovery and Git enrichment run under directory/entry/project/item bounds, monotonic deadlines, cooperative cancellation, single-flight slots, and capped output. Discovery cost is bounded by configuration, not by repository size.
- **No schema migration.** MC-6.12 reuses the existing `project_samples` table; deployment therefore requires no migration step and rollback requires none either.
- **No automatic host permission repair.** Permission residuals are documented (see [Known host residual](#known-host-residual)); repairing them is an explicit host-provisioning decision because automated repair would mask ownership drift and can regress under routine `mina` activity.
- **Telemetry failure degrades dashboard freshness, never the dashboard.** A refresh failure is logged and swallowed; the overview response is always produced. Freshness correctly reports `stale`/`never_sampled` instead of lying about health.

## Future hardening / follow-ups (NON-BLOCKING)

None of the items below block production use of the MC-6.12 telemetry remediation. They are candidate improvements supported by the audit — recorded here so they are not lost, not scheduled.

- Possible SQL-side latest-per-project optimization: `get_latest_project_samples()` currently scans recent rows ordered by `sampled_at DESC` and deduplicates per project in Python; `idx_project_samples_name_time` already exists and a grouped SQL query could push the dedup into the engine.
- Possible SQLite busy_timeout/WAL hardening: dashboard read connections currently rely on journal-mode DELETE with no `busy_timeout`; concurrent dashboard reads could surface `SQLITE_BUSY` under write pressure.
- Direct regression test for the `AIPM_LOG_FILE` override in the logs capability.
- Coordinator-level timeout/cancellation integration test covering a real bounded operation end-to-end.
- MC-3 invalid-timestamp regression test for the service-health latest-event selection.
- Concurrent dashboard refresh race test (two simultaneous `/api/overview` requests crossing the refresh interval).
- Documentation/clarification of branch-null vs dirty semantics in API responses when Git metadata is only partially readable.

## References

- [`CURRENT_STATUS.md`](CURRENT_STATUS.md) — canonical current-state reconciliation.
- [`MC-TELEMETRY_STABILITY.md`](MC-TELEMETRY_STABILITY.md) — historical remediation record for discovery bounds and Git safety limits (predecessor workstream).
- [`MISSION_CONTROL.md`](MISSION_CONTROL.md) — historical Mission Control architecture and milestone narrative.
- [`MC-6.12_PRODUCTION_ARCHITECTURE.md`](MC-6.12_PRODUCTION_ARCHITECTURE.md) / [`MC-6.12_PRIVILEGE_BOUNDARY.md`](MC-6.12_PRIVILEGE_BOUNDARY.md) — the separate MC-6.12 executor/action plane.
- Repository evidence at `ee308ac600f2148166efa1146a455b6bc1fe2a06` (working tree contains the remediation patch): `ops/systemd/aipm-{telemetry,dashboard}.service`, `src/aipm/providers/git/provider.py`, `src/aipm/services/project/service.py`, `src/aipm/services/telemetry/project.py`, `src/aipm/repositories/telemetry/{base,sqlite}.py`, `src/aipm/capabilities/dashboard/{api,logs_api,service_health_api}.py`, `src/aipm/providers/logs.py`.