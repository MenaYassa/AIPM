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

The dashboard needs read access to the Docker socket to inspect containers. That permission is powerful. Prefer a dedicated host policy and a protected Cloudflare Access application over exposing the port directly to the internet.

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

MC-4.5 hardens notification retries, suppression windows, delivery claims, schema integrity, retention, UNKNOWN reconciliation, metrics, and startup validation. Review [`MC-4.5_PRODUCTION_RUNBOOK.md`](MC-4.5_PRODUCTION_RUNBOOK.md) before enabling notifications. The dashboard remains loopback-only by default; public access requires independently verified authenticated Cloudflare Access or equivalent ingress. No production Cloudflare or systemd mutation is performed by this milestone.


## MC-2.1 Telemetry Performance & Sampling

MC-2.1 separates the 15-second fast state loop from independently scheduled, single-flight slow Docker resource and project refresh tasks. See [`MC-2.1_TELEMETRY_PERFORMANCE.md`](MC-2.1_TELEMETRY_PERFORMANCE.md) for configuration, freshness semantics, sparse resource history, acceptance tests, and rollback guidance.


## Current Mission Control status and next steps

**Updated:** 2026-08-25

Mission Control’s read-only cockpit implementation is complete through **MC-6.8**. The current repository checkpoint additionally includes MC-6.13 Phases 2, 3, 4A, 4B, and 4D, pushed at `d90d32f54edc5abf373ecd0308b4963e9a6cabcc`. The cockpit includes the shared MC-6 foundation and vanilla shell, Server/Host Intelligence, Docker intelligence, Project/Application Intelligence, allow-listed Systemd observation, bounded redacted Logs, and the existing telemetry/history/event/incident/notification read projections. MC-6.13 Phase 2/3/4A remains pure advisor domain composition; Phase 4B adds only a private authenticated advisor API; and Phase 4D adds only a private-VPS telemetry-owned bounded export plus transport-neutral adapter for CPU, memory, and disk, ending at `AdvisorCompositionRequest` without dashboard integration.

MC-6.4 was reconciled rather than duplicated because Server Intelligence was already delivered through MC-6.3. MC-6.6.1 through MC-6.6.3 refined association correctness, taxonomy, filtering, and health evidence. MC-6.7.1 reconciled the seven-entry Systemd registry: Cloudflared is Docker-owned and is not represented as Systemd unless a genuine allow-listed unit exists.

MC-6.13 Phase 4B.1, Phase 4C, and Phase 4E remain future, unauthorized, and not started; Phase 4B is complete as a private authenticated, read-only transport boundary, and Phase 4D is complete as a bounded typed non-runtime adapter. Neither implies dashboard UI, public exposure, live polling, LLM/provider functionality, advisor evaluation, or actions.

### Current operational gates

Repository advisor work does not authorize or imply target-VPS application deployment. Runtime validation and any service rollout remain separate operational gates. The current dashboard ingress architecture is Cloudflared container → `172.20.0.1:8788` → host nginx reverse proxy → `127.0.0.1:8787`, serving `vpanel.03092017.xyz`. The dashboard remains loopback-bound; Cloudflared/Docker configuration, credentials, live SQLite, and Systemd runtime changes are outside the MC-6.13 repository scope.

claims that Phase 4A or Phase 4B changed the host.

The successful Gate 2.1 staging harness remains preserved at:

```text
ops/staging/mc5-gate2.1-staging-v2.sh
SHA-256: 9e12cdc01f901381ff34b16dd68c11a14cf1158e1c32bbde928bce13c6c238e7
```

The complete current ledger is in [`MC-6_STATUS.md`](MC-6_STATUS.md), the milestone roadmap is in [`MC-6_IMPLEMENTATION_PLAN.md`](MC-6_IMPLEMENTATION_PLAN.md), and the broader AIPM update-management roadmap remains in [`../PRODUCTION_ROADMAP.md`](../PRODUCTION_ROADMAP.md).
