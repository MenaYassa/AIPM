# Mission Control MC-1.5 Completion Report

## 1. Scope completed

MC-1.5 refactored Mission Control from a FastAPI prototype with infrastructure logic inside `server.py` into a read-only AIPM telemetry subsystem. The current Mission Control frontend and `/api/overview` response contract were preserved.

The implementation stops at current telemetry. It does not add historical storage, SQLite, charts, Incident Room, alerts, notifications, guarded actions, Docker log streaming, AIPM update execution, Cloudflare API credentials, Cloudflare account management, or AI diagnostics.

## 2. Files created

| File | Purpose |
|---|---|
| `src/aipm/models/telemetry.py` | Typed telemetry domain models, including `HostSnapshot`, `ResourceStats`, `ContainerSnapshot`, `DockerSnapshot`, `ProjectSnapshot`, `ProjectInventorySnapshot`, `TunnelSnapshot`, `HandbookRoute`, and `DashboardSnapshot`. |
| `src/aipm/services/telemetry/__init__.py` | Telemetry package boundary. |
| `src/aipm/services/telemetry/host.py` | Host telemetry over the existing `SystemService`, plus swap, load, uptime, and network measurements. |
| `src/aipm/services/telemetry/docker.py` | Read-only Docker telemetry over the existing Docker service/provider boundary. |
| `src/aipm/services/telemetry/project.py` | Read-only project inventory over the existing `ProjectService`. |
| `src/aipm/services/telemetry/tunnel.py` | Local-only cloudflared Docker/systemd detection. |
| `src/aipm/services/telemetry/dashboard.py` | Focused aggregation service that collects, aggregates, and isolates component failures. |
| `src/aipm/mappers/dashboard.py` | Maps typed snapshots to the stable frontend JSON contract and safe diagnostics. |
| `src/aipm/capabilities/dashboard/__init__.py` | Dashboard capability package. |
| `src/aipm/capabilities/dashboard/api.py` | Meaningful dashboard capability façade that wires shared AIPM dependencies once. |
| `src/aipm/capabilities/dashboard/routes.py` | Typed handbook route metadata. |
| `tests/test_telemetry.py` | Mocked telemetry and architecture-boundary tests. |
| `docs/MC-1.5_COMPLETION_REPORT.md` | This completion report. |

## 3. Files modified

| File | Change |
|---|---|
| `src/aipm/dashboard/server.py` | Reduced to FastAPI route handling, static serving, app construction, and delegation to `DashboardApi`. Direct `psutil`, Docker SDK, subprocess, project discovery, and infrastructure logic were removed. |
| `src/aipm/providers/docker/provider.py` | Added a read-only one-shot stats boundary with existing Docker exception translation. |
| `src/aipm/mappers/docker.py` | Added resource-stat and `ContainerSnapshot` mapping while reusing the existing `Container` model. |
| `src/aipm/services/docker/service.py` | Made provider construction lazy so Application-based dashboard construction remains valid when Docker is unavailable. |
| `tests/test_dashboard.py` | Made HTTP tests deterministic through injected fake dashboard capability data while preserving health, overview, and static-page coverage. |
| `docs/MISSION_CONTROL.md` | Updated deployment, architecture, safe diagnostics, read-only boundary, test, and future-scope documentation. |
| `README.md` | Documented the MC-1.5 architecture and dashboard command. |
| `pyproject.toml` | Retained the dashboard runtime dependencies introduced in the initial Mission Control implementation. |
| `src/aipm/cli/app.py` | Retained the `aipm dashboard` command as the supported launcher. |

## 4. Files removed

No files were removed.

## 5. Architecture before and after

### Before

```text
FastAPI route
  ├── psutil / platform / socket / os
  ├── DockerProvider + raw Docker SDK attributes
  ├── container.stats()
  ├── systemctl subprocess
  ├── ProjectService discovery and response shaping
  └── raw nested dictionaries
```

The former server duplicated `SystemService` behavior, read raw Docker SDK objects directly, mixed subprocess logic with presentation, and returned infrastructure dictionaries without a domain snapshot boundary.

### After

```text
FastAPI routes
      ↓
DashboardApi capability façade
      ↓
DashboardTelemetryService
      ├── HostTelemetryService
      │     └── existing SystemService + read-only psutil measurements
      ├── DockerTelemetryService
      │     └── existing DockerService → DockerProvider → DockerMapper
      ├── ProjectTelemetryService
      │     └── existing ProjectService → GitProvider → Project/GitRepository
      └── TunnelTelemetryService
            └── local Docker snapshot + systemd detection
      ↓
DashboardSnapshot
      ↓
DashboardResponseMapper
      ↓
Stable /api/overview JSON
```

The dashboard aggregation service does not contain Docker, Git, Compose, project discovery, health, or Cloudflare business logic. The telemetry layer is strictly read-only and does not fetch, pull, checkout, stash, reset, clean, start, stop, restart, remove, prune, mutate Compose, restart systemd, change packages, or mutate Cloudflare.

## 6. Tests added

The new mocked test coverage includes host telemetry, Docker availability, typed Docker mapping, per-container stats failure preservation, Docker outage isolation, cloudflared detection through Docker, cloudflared detection through systemd, unknown tunnel state, project discovery success, project discovery failure, dashboard aggregation failure isolation, safe error mapping, stable frontend response shape, and thin-server dependency checks.

The route tests cover `/healthz`, `/api/overview`, and `/` without requiring a live Docker daemon or production VPS. The telemetry tests use fake providers, fake services, fake process execution, and deterministic host measurements.

## 7. Full test result

```text
28 passed, 1 warning in 1.62s
```

The only warning is the existing Starlette/httpx deprecation warning emitted by the installed test-client stack. It does not fail the suite or affect runtime behavior.

## 8. `git diff --check` result

```text
git diff --check: passed
```

The final audit also found no forbidden state-changing operations in the executable telemetry, dashboard capability, or dashboard mapper Python layers. The thin FastAPI server contains no direct `psutil`, `DockerProvider`, `ProjectService`, `subprocess`, or Git infrastructure imports.

## 9. Dashboard verification result

The dashboard was started locally and verified through the live HTTP server:

| Check | Result |
|---|---|
| `GET /healthz` | Passed; returned `{"status":"ok"}`. |
| `GET /api/overview` | Passed; returned `generated_at`, `host`, `docker`, `tunnel`, `projects`, and `handbook`. |
| Static page | Passed; served the Mission Control interface containing `AIPM Mission Control` and `Service pulse`. |
| `aipm --help` | Passed; exposes the `dashboard` command. |
| Local Docker behavior | Correctly reported Docker unavailable in the sandbox without breaking host/project telemetry. |
| Local tunnel behavior | Reported the local sandbox’s detected state without querying Cloudflare account APIs. |

No production VPS operation, Docker mutation, Git mutation, systemd mutation, package mutation, Cloudflare mutation, or GitHub push was performed.

## 10. Technical debt intentionally left

The dashboard still performs request-time project discovery and Git inspection. This is intentionally left without caching until timing is measured on the actual VPS. Docker stats are best-effort per running container and are intentionally not persisted.

The existing `Container` model stores ports as strings for compatibility with current callers. Resource statistics are composed in `ContainerSnapshot` rather than expanding the existing model, which avoids a breaking change but leaves two related domain objects to understand.

The current dependency stack emits a test-client deprecation warning. It should be resolved in a separate dependency-maintenance change rather than mixed into MC-1.5.

Cloudflared detection is local-only. A local process or container being healthy does not prove remote Cloudflare account status, and MC-1.5 intentionally does not add a credentialed broker.

The static handbook metadata remains a small typed capability-owned list rather than a full content registry. The original uploaded handbook remains the richer operational reference.

## 11. Recommended MC-2 architecture

MC-2 should add a separate sampling boundary rather than placing history inside request-time telemetry collection:

```text
DashboardTelemetryService
      ↓ current DashboardSnapshot
TelemetrySampler
      ↓ bounded samples
HistoryRepository
      ↓
Incident/Event derivation
      ↓
Dashboard read APIs
```

The recommended first historical milestone is a bounded local time-series store with a configurable retention period, beginning with 24 hours and expanding only after measuring VPS disk and request costs. Sampling should run independently from HTTP requests, preserve the same typed `DashboardSnapshot` input, and write only sanitized domain measurements.

After history is stable, an Incident Room can derive events from deterministic threshold and state-transition rules. Guarded operations should remain separate and continue through AIPM’s existing update planner, safety gates, approval, execution, verification, and audit path. AI may eventually explain deterministic findings, but it must not bypass those safety rules.
