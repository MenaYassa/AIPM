# AIPM Mission Control

Mission Control is the Handbook 2.0 capability of AIPM. MC-1.5 keeps the current interface and JSON contract while moving infrastructure inspection into typed, read-only AIPM telemetry services.

## MC-1.5 architecture

The current request path is:

```text
FastAPI routes
      ↓
DashboardApi capability façade
      ↓
DashboardTelemetryService
      ↓
HostTelemetryService · DockerTelemetryService · ProjectTelemetryService · TunnelTelemetryService
      ↓
Existing SystemService · DockerProvider/Mapper · ProjectService/GitProvider · local systemd/container detection
```

The domain flow is:

```text
External systems
      ↓
Existing providers and mappers
      ↓
Existing AIPM domain models + telemetry-specific measurements
      ↓
DashboardSnapshot
      ↓
DashboardResponseMapper
      ↓
Stable /api/overview JSON
```

FastAPI no longer owns host, Docker, Git, project, or tunnel business logic. The application context constructs shared AIPM services once when the app is created, and each overview request collects a fresh read-only snapshot through those services.

## What is included

The dashboard exposes `/api/overview` and `/healthz`. The interface refreshes every 15 seconds and presents CPU, memory, swap, root-disk usage, load, uptime, network connection count, container state, best-effort container CPU and memory usage, project inventory, local cloudflared visibility, and handbook routes.

The domain layer uses existing AIPM models wherever possible. `ContainerSnapshot` composes the existing `Container` model with resource measurements rather than duplicating identity, image, state, health, ports, labels, stack, and creation fields. Project telemetry returns existing `Project` objects enriched by the established read-only Git path.

## Read-only boundary

MC-1.5 telemetry never performs state-changing operations. It does not run Git fetch, pull, checkout, stash, reset, or clean. It does not start, stop, restart, remove, prune, or mutate Docker or Compose resources. It does not restart systemd units, change packages, modify Cloudflare configuration, or use Cloudflare credentials.

Remote SHA, ahead, and behind values represent the currently known local and remote-tracking state. A fetch, if ever needed, must be an explicit operator action outside the dashboard snapshot path.

> The dashboard observes the current VPS state; it does not repair, update, or mutate it.

## Failure isolation and diagnostics

Host telemetry continues when Docker is unavailable. Docker stats failures preserve the affected container row and mark only its resource measurements unavailable. Project discovery failures preserve host and Docker telemetry. Tunnel detection failures produce `unknown` rather than crashing the overview.

Detailed exceptions are sent to the existing AIPM logger. HTTP responses expose only safe structured diagnostics such as `Docker telemetry unavailable`; they do not return tracebacks, credentials, environment variables, tokens, command secrets, or unnecessary private paths.

## Local development

From the repository root:

```bash
python3 -m pip install -e '.[dev]'
aipm dashboard
```

The default listener is `127.0.0.1:8787`. Open `http://127.0.0.1:8787/` on the VPS or through an SSH port-forward.

The equivalent explicit command is:

```bash
aipm dashboard --host 127.0.0.1 --port 8787
```

Keep the listener loopback-only. Do not bind the dashboard to `0.0.0.0:8787`. The current bridge-bound host nginx listener is the controlled ingress point for the containerized tunnel.

## Cloudflared integration

The current deployed ingress path is:

```text
Cloudflared container
    -> 172.20.0.1:8788
    -> host nginx reverse proxy
    -> 127.0.0.1:8787
    -> AIPM dashboard
```

The public hostname is `vpanel.03092017.xyz`. The Cloudflared origin remains the bridge-side endpoint `http://172.20.0.1:8788`; nginx forwards only to the loopback dashboard. Do not document or configure the containerized tunnel to use `http://127.0.0.1:8787`, because that targets the container’s own loopback namespace rather than the host dashboard.

Do not put a Cloudflare API token in the AIPM repository or in the browser. Cloudflared and Docker remain infrastructure-owned and are not modified by Mission Control advisor work.

## Suggested systemd unit

After copying the repository to the VPS and creating its virtual environment, use a dedicated unprivileged service account where practical. The following unit is a template; adjust paths and the user to match the VPS:

```ini
[Unit]
Description=AIPM Mission Control
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=aipm
WorkingDirectory=/opt/AIPM
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/AIPM/.venv/bin/aipm dashboard --host 127.0.0.1 --port 8787
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=read-only
ReadWritePaths=/var/lib/aipm

[Install]
WantedBy=multi-user.target
```

The dashboard needs read access to the Docker socket to inspect containers. That permission is powerful. The documented public hostname is protected by the confirmed Cloudflare Access edge boundary; retain a dedicated host policy and do not expose the dashboard port directly to the internet.

## Verification checklist

Run the following checks after installation:

```bash
curl -fsS http://127.0.0.1:8787/healthz
curl -fsS http://127.0.0.1:8787/api/overview
systemctl status aipm-mission-control --no-pager
```

Confirm that the page loads through the intended tunnel hostname, that host values match the VPS, Docker availability is reported correctly, and the local cloudflared card shows the expected agent. If Docker is unavailable, inspect the service account’s read access to `/var/run/docker.sock` rather than broadening network exposure.

## Test coverage

The MC-1.5 suite uses mocked external systems and does not require a production VPS or live Docker daemon. It covers host telemetry, Docker availability, per-container stats failures, tunnel detection through Docker and systemd, unknown tunnel state, project discovery success and failure, dashboard aggregation failure isolation, stable response shape, and the absence of infrastructure logic in FastAPI.

Run the full suite with:

```bash
pytest -q
git diff --check
```

## Deliberately deferred

MC-1.5 stops after the telemetry architecture refactor. Historical telemetry, SQLite, charts, Incident Room, alerts, notifications, guarded operations, Docker log streaming, AIPM update execution, Cloudflare API access, and AI diagnostics belong to later milestones and are not part of this release.


## MC-2 historical telemetry

MC-2 adds normalized historical telemetry without changing the read-only infrastructure boundary. The sampler consumes the typed `DashboardSnapshot`, maps it into normalized rows, and writes only to AIPM’s own SQLite database.

```text
DashboardTelemetryService
      ↓
DashboardSnapshot
      ↓
TelemetrySampler
      ↓
TelemetryHistoryMapper
      ↓
SQLiteHistoryRepository
      ↓
SQLite
```

The default database is:

```text
~/.local/state/aipm/telemetry/mission_control.db
```

The path is configurable through YAML or `AIPM_TELEMETRY_DB`. Example configuration:

```yaml
telemetry:
  enabled: true
  interval_seconds: 15
  retention_days: 1
  database_path: ~/.local/state/aipm/telemetry/mission_control.db
```

`interval_seconds` and `retention_days` must both be greater than zero. An empty path or a path that resolves to an existing directory is rejected during configuration loading. UTC is used internally for timestamps and at domain/API boundaries.

### Sampler commands

Collect one sample:

```bash
aipm telemetry sample
```

Run the dedicated sampler process:

```bash
aipm telemetry run
```

`aipm dashboard` does not start the sampler. The sampler handles SIGTERM and SIGINT by requesting a graceful stop; a cycle already inside a database transaction is allowed to finish, and the process closes its database connection before exiting. Production should run exactly one sampler process through systemd rather than embedding a background thread in HTTP workers.

Suggested unit template:

```ini
[Unit]
Description=AIPM Mission Control telemetry sampler
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=aipm
WorkingDirectory=/opt/AIPM
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/AIPM/.venv/bin/aipm telemetry run
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
```

This unit is a template only. MC-2 implementation did not install or enable it on any VPS.

### Historical schema

The SQLite database contains `sample_runs`, `host_samples`, `container_samples`, `project_samples`, and `tunnel_samples`. It has foreign keys enabled, WAL enabled where supported, parameterized SQL, short transactions, and timestamp-based retention. Indexes cover sample timestamps, container identity/name plus timestamp, project name plus timestamp, and tunnel timestamp.

The database does not contain an opaque `DashboardSnapshot` JSON blob, Incident/Event tables, alert state, AI output, or operational commands. Historical container rows retain both `container_id` and `container_name`; lifecycle identity modeling is deferred to MC-3.

Retention deletes rows older than `now - retention_days` using each table’s `sampled_at` value. It does not use `sample_runs.id`, and it deletes only rows inside the telemetry database.

### Historical API

The existing endpoints remain unchanged:

```text
GET /healthz
GET /api/overview
GET /
```

MC-2 adds:

```text
GET /api/history/host?range=1h&limit=500
GET /api/history/containers?name=<container>&range=24h&limit=500
GET /api/history/projects?name=<project>&range=24h&limit=500
GET /api/history/tunnel?range=24h&limit=500
```

Supported ranges are `1h`, `6h`, `24h`, and `7d`; limits are bounded to 1–5000. History responses use safe structures with `available`, `status`, `error`, and `points`. SQLite failures do not break `/api/overview`; history failures return an unavailable response and detailed diagnostics go through the shared AIPM logger.

### Minimal UI proof

The existing Mission Control visual design remains intact. MC-2 adds a compact **Historical pulse** panel with CPU, memory, and disk SVG trends and `1H`, `6H`, and `24H` selectors. The panel is proof of persisted history only; it does not add Incident Room, alerting, notifications, controls, or a visual redesign.


## MC-3 Event Engine & Incident Room

MC-3 derives deterministic events and incidents from committed MC-2 telemetry facts. It does not change the telemetry sampler’s responsibility.

```text
Persisted Dashboard facts
      ↓
HistoricalFrameService
      ↓
HealthEngine evidence + EventDerivationService
      ↓
EventRepository
      ↓
IncidentEngine
      ↓
IncidentRepository
      ↓
Event/Incident API and Incident Room
```

The event processor never directly calls psutil, Docker, Compose, Git, systemd, or Cloudflare. It consumes typed historical rows and the existing Health Engine’s deterministic output. It never performs remediation.

### Event processor commands

Process all pending committed telemetry runs once:

```bash
aipm events process
```

Process one source telemetry run:

```bash
aipm events process --run-id 42
```

Run the dedicated event processor:

```bash
aipm events run
```

The event processor is not started by FastAPI. Production should run exactly one `aipm events run` process through systemd, separately from `aipm telemetry run`.

Suggested unit template:

```ini
[Unit]
Description=AIPM Mission Control event processor
After=aipm-telemetry.service

[Service]
Type=simple
User=aipm
WorkingDirectory=/opt/AIPM
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/AIPM/.venv/bin/aipm events run
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
```

This unit is a template only. MC-3 implementation did not install, enable, or modify systemd on any VPS.

### Initial deterministic event families

MC-3 currently derives container start/restarting/restarted/stopped/recovered/health-changed events, project Git state changes, local tunnel state changes, project HealthState changes, and HealthEngine finding-set changes. It does not infer lifecycle across different container IDs, create threshold alerts, or claim causality.

Repeated observations do not create repeated events. Event identity includes source run, previous run, event type, resource, and transition values. The source-run processing marker and unique event key make retries idempotent.

### Incident correlation

Incidents use explicit correlation keys:

```text
container:{container_id}:stability
project:{project_path}:git
project:{project_path}:health
tunnel:local:availability
```

Opening events update an existing open incident with the same key. Recovery events resolve the matching incident. Acknowledgement changes only incident metadata and never executes infrastructure actions.

### MC-3 API

Existing MC-2 routes remain unchanged. MC-3 adds:

```text
GET /api/events
GET /api/events/{id}
GET /api/incidents
GET /api/incidents/{id}
POST /api/incidents/{id}/acknowledge  (underlying capability only; not mounted in the current read-only dashboard)
```

Event filters include `range`, `severity`, `event_type`, `resource_type`, `resource_id`, and bounded `limit`. Incident filters include `range`, `status`, `severity`, `resource_id`, and bounded `limit`. API failures return safe structured responses and do not expose SQL or tracebacks.

### Incident Room

The existing Mission Control visual language now includes a focused Incident Room section. It shows open incidents, severity, status, resource, start time, summary, and the latest persisted event timeline. It contains no restart, stop, start, update, rollback, shell, Docker exec, backup, restore, alerting, or AI controls.

### Event/incident storage

MC-3 reuses the MC-2 SQLite database. It adds `event_processing_runs`, `health_observations`, `health_findings`, `events`, `event_evidence`, `incidents`, and `incident_events`. Telemetry tables remain unchanged.

Event and incident retention is independent from high-frequency telemetry retention. The schema supports a later configurable event policy, but long-term cleanup should remain disabled until representative event volume is measured. Open and acknowledged incident evidence must not be deleted by retention.


## MC-4.5 production hardening

MC-4.5 hardens notification retries, suppression windows, delivery claims, schema integrity, retention, UNKNOWN reconciliation, metrics, and startup validation. Review [`MC-4.5_PRODUCTION_RUNBOOK.md`](MC-4.5_PRODUCTION_RUNBOOK.md) before enabling notifications. The dashboard remains loopback-only by default; the documented public hostname is protected by the confirmed Cloudflare Access edge boundary. No production Cloudflare or systemd mutation is performed by this milestone.


## MC-2.1 Telemetry Performance & Sampling

MC-2.1 separates the 15-second fast state loop from independently scheduled, single-flight slow Docker resource and project refresh tasks. See [`MC-2.1_TELEMETRY_PERFORMANCE.md`](MC-2.1_TELEMETRY_PERFORMANCE.md) for configuration, freshness semantics, sparse resource history, acceptance tests, and rollback guidance.


## Current Mission Control status and next steps

**Updated:** 2026-08-26

Mission Control’s read-only cockpit implementation is complete through **MC-6.8**. The current repository checkpoint additionally includes MC-6.13 Phases 2, 3, 4A, 4B, fixture-only 4C presentation, live read-only 4C.1 orchestration, 4C.2 sample-boundary alignment, 4C.3 complete-evidence validation, 4D, and 4E, pushed at `ead26b68155baee6c38e1f47ad124ae676ea56f7`. The cockpit includes the shared MC-6 foundation and vanilla shell, Server/Host Intelligence, Docker intelligence, Project/Application Intelligence, allow-listed Systemd observation, bounded redacted Logs, the existing telemetry/history/event/incident/notification read projections, and the advisor live/fixture presentation on `#/ai-agent`. MC-6.13 Phase 2/3/4A remains pure advisor domain composition; Phase 4B adds only a private authenticated advisor API; Phase 4D adds only a private-VPS telemetry-owned bounded export plus transport-neutral adapter for CPU, memory, and disk, ending at `AdvisorCompositionRequest`; Phase 4C.1 adds the server-owned live read-only `GET /api/advisor` path through that export, adapter, and composition boundary; Phase 4C.2 aligns evaluation to a completed telemetry sample boundary without changing the five-minute completeness contract; and Phase 4E adds the bounded additive `resource_history_summary` presentation field.

MC-6.4 was reconciled rather than duplicated because Server Intelligence was already delivered through MC-6.3. MC-6.6.1 through MC-6.6.3 refined association correctness, taxonomy, filtering, and health evidence. MC-6.7.1 reconciled the seven-entry Systemd registry: Cloudflared is Docker-owned and is not represented as Systemd unless a genuine allow-listed unit exists.

MC-6.13 Phase 4C remains the explicit fixture-driven presentation capability on the existing `#/ai-agent` route, while Phase 4C.1 is landed as the live read-only `GET /api/advisor` path. Phase 4C.2 aligns live evaluation to a completed telemetry sample boundary, and Phase 4E adds only the bounded additive `resource_history_summary` derived from preserved typed evidence and rendered separately from findings and recommendations. Cloudflare Access protects the documented public ingress, and AIPM relies on that private edge authentication boundary without implementing JWT verification, identity middleware, session storage, or proxy-header trust. The live path uses a server-owned evaluation context, a bounded five-minute telemetry export, the Phase 4D adapter, and the Phase 4A composition boundary; it provides no polling, actions, approvals, remediation, or LLM/provider functionality. Phase 4B remains a private authenticated, read-only transport boundary and Phase 4D remains the telemetry-owned typed export/adapter boundary. Stronger application identity behavior remains future, separately authorized work.

### Current operational gates

Repository advisor work does not authorize or imply target-VPS application deployment. Runtime validation and any service rollout remain separate operational gates. The current dashboard ingress architecture is Cloudflared container → `172.20.0.1:8788` → host nginx reverse proxy → `127.0.0.1:8787`, serving `vpanel.03092017.xyz`; Cloudflare Access is the confirmed edge authentication boundary for that public hostname. AIPM relies on the private edge protection and does not verify Cloudflare JWTs or identity headers. The dashboard remains loopback-bound; Cloudflared/Docker configuration, credentials, live SQLite, and Systemd runtime changes are outside the MC-6.13 repository scope.

### MC-6.13 Phase 4C.1 production completeness capture

The supplied read-only production capture used the canonical telemetry database `/home/mina/.local/state/aipm/telemetry/mission_control.db`, a deployed telemetry cadence of `60s`, and a `300s` evaluation window. Five sample runs were available, spanning `240s`. CPU, memory, and disk each reported `insufficient` with reason `insufficient_coverage`, while `invalid_source_rows=0`. The result is expected because the valid history did not span the complete five-minute window.

### MC-6.13 Phase 4E production validation

The deployed Phase 4E commit is `ead26b68155baee6c38e1f47ad124ae676ea56f7`. The operator performed only the authorized `aipm-dashboard.service` restart. The post-restart public authenticated `GET https://vpanel.03092017.xyz/api/advisor` returned HTTP 200 with `resource_history_summary` present. The initial response before restart lacked the additive field because the old dashboard process was still running; this was a deployment-process state, not a Phase 4E implementation defect.

The validated response was `status=fresh`, `available=true`, and `evaluation_time == generated_at` at `2026-08-26T18:02:10+00:00`, with zero uncertainties, zero findings, and zero recommendations. The three summaries were deterministic and complete, with no `maximum_gap` exposure:

| Metric | State | Valid points | Temporal span | Cadence | Peak | Peak observed at |
|---|---|---:|---:|---:|---:|---|
| CPU | `complete` | 6 | 300s | 60s | 30.2% | `2026-08-26T17:57:10+00:00` |
| Memory | `complete` | 6 | 300s | 60s | 58.8% | `2026-08-26T17:57:10+00:00` |
| Disk | `complete` | 6 | 300s | 60s | 56.4% | `2026-08-26T17:57:10+00:00` |

Complete evidence is not a health claim. It indicates that the bounded temporal and validity prerequisites were met; it does not itself create a finding or recommendation. The zero-result response is correct for this complete low-pressure evaluation. Incomplete, stale, unavailable, and invalid evidence semantics remain explicit and fail closed. No database, telemetry configuration, source, infrastructure, authentication, Cloudflare, Systemd unit, Docker, nginx, or other runtime modification was reported.

The advisor UI’s `history 15/15` is source coverage, not temporal completeness. It represents fifteen observed history metric records out of fifteen expected records, not fifteen points per metric or a five-minute span. The three identical `missing_evidence — Resource-history window for agent is incomplete` records are expected, one for each incomplete CPU, memory, and disk history envelope. No Phase 3 completeness semantics were changed.

The legacy `/home/ubuntu/.local/state/aipm/telemetry/mission_control.db` ownership investigation is closed unresolved and must not infer inactivity from sandbox evidence:

```text
ACTIVE_CONSUMER=UNKNOWN
PROCESS=UNKNOWN
SERVICE=UNKNOWN
DOCKER_CONSUMER=UNKNOWN
DATABASE_ACTION_REQUIRED=NO
```

The canonical managed writer and dashboard/advisor reader path remains the mina database path. No deletion, permission change, service change, or database action is implied.

claims that Phase 4A or Phase 4B changed the host.

The successful Gate 2.1 staging harness remains preserved at:

```text
ops/staging/mc5-gate2.1-staging-v2.sh
SHA-256: 9e12cdc01f901381ff34b16dd68c11a14cf1158e1c32bbde928bce13c6c238e7
```

The complete current ledger is in [`MC-6_STATUS.md`](MC-6_STATUS.md), the milestone roadmap is in [`MC-6_IMPLEMENTATION_PLAN.md`](MC-6_IMPLEMENTATION_PLAN.md), and the broader AIPM update-management roadmap remains in [`../PRODUCTION_ROADMAP.md`](../PRODUCTION_ROADMAP.md).
