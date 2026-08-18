# MC-6.3 — Server & Host Intelligence Design

## Status and scope

This is a **design and inspection report only**. MC-6.3 implementation has not started. No source files, tests, production files, systemd configuration, database, WAL/SHM files, Docker state, Cloudflare state, credentials, providers, telemetry runtime, MC-3 runtime, notifications, or services were modified or accessed.

The inspected repository checkpoint is:

```text
HEAD        = ad7ac5866b88eb1898254ab4ae9175b8fe92b613
origin/main = ad7ac5866b88eb1898254ab4ae9175b8fe92b613
```

The MC-5 Gate 2.1 harness remains preserved and unmodified at SHA-256:

```text
9e12cdc01f901381ff34b16dd68c11a14cf1158e1c32bbde928bce13c6c238e7
```

> **Design principle:** reuse the existing host telemetry and Dashboard capability path before creating any new provider, repository, schema, worker, or deployment surface.

## 1. Current architecture inventory

### Existing data flow

The current host path is already layered and suitable for a dedicated Server projection:

```text
SystemService / psutil / stdlib
        ↓
HostTelemetryService.snapshot()
        ↓
DashboardTelemetryService._collect_host()
        ↓
DashboardTelemetryService.fast_snapshot()
        ↓
DashboardApi.overview()
        ↓
DashboardResponseMapper._host()
        ↓
GET /api/overview
        ↓
MC-6.2 Dashboard view
```

Historical host data follows the existing telemetry persistence path:

```text
DashboardSnapshot
        ↓
TelemetryHistoryMapper._host()
        ↓
existing telemetry repository / historical sample
        ↓
HistoricalQueryService.host()
        ↓
DashboardHistoryApi.host()
        ↓
GET /api/history/host
        ↓
Dashboard Resource History
```

Current host collection is intentionally isolated from Docker, project discovery, and tunnel collection. `DashboardTelemetryService.fast_snapshot()` collects the host first and does not wait for slow Docker resource statistics or project discovery. That fast/slow split must remain unchanged.

### Relevant existing components

| Layer | Existing component | Current responsibility | MC-6.3 decision |
|---|---|---|---|
| System/provider boundary | `SystemService` | Hostname, OS, kernel, architecture, Python version, CPU cores/utilization, memory, and root disk through standard-library and psutil calls. | Reuse; extend only for explicitly approved missing host fields. |
| Host telemetry | `HostTelemetryService` | Composes `SystemService.summary()`, swap, load averages, uptime, interface count, and established connection count. | Reuse as the authoritative host collector. |
| Dashboard aggregation | `DashboardTelemetryService` | Isolates host, Docker, project, and tunnel observations; provides legacy and fast snapshot paths. | Extend with a host/server projection method only if needed; do not duplicate host collection. |
| Domain models | `SystemSummary`, `HostInfo`, `CpuInfo`, `MemoryInfo`, `DiskInfo`, `HostSnapshot`, `NetworkStats`, `TelemetryError`, `TelemetryFreshness` | Typed host and telemetry contracts. | Extend existing models or add a narrowly scoped host detail model; do not create a second telemetry model hierarchy. |
| Dashboard façade | `DashboardApi` | Builds read-only telemetry services and maps the overview. | Preserve `/api/overview`; add a separate Server façade only when the additive contract is approved. |
| Mapper | `DashboardResponseMapper._host()` | Maps current host data to the stable overview payload. | Do not change existing `/api/overview` semantics; add a separate Server mapper for richer fields. |
| History | `TelemetryHistoryMapper._host()`, `HistoricalQueryService`, `DashboardHistoryApi` | Persists and queries aggregate host history. | Reuse for existing trends; no new history database. |
| Frontend | MC-6.2 vanilla shell, `mission-control-state.js`, `mission-control-scheduler.js` | Hash navigation, shared state classes, bounded polling, Dashboard view, and safe placeholders. | Add a Server view only after the API contract is approved. |
| Tests | `tests/test_telemetry.py`, `tests/test_dashboard.py`, `tests/test_history_api.py`, MC-6.1/6.2 tests | Prove host collection, failure isolation, stable overview keys, history behavior, routing, and read-only posture. | Extend with isolated Server façade/provider/mapper/UI tests. |

## 2. Existing Server/host data inventory

The current `/api/overview` already exposes a substantial Server payload. The existing mapper returns host availability/status/error, identity, uptime, load, CPU, memory, swap, aggregate root disk, and aggregate network information.

### Host identity

The existing `SystemService` provides:

- `hostname` through `socket.gethostname()`.
- `os` through `platform.system()`.
- `kernel` through `platform.release()`.
- `architecture` through `platform.machine()`.
- Python version through `platform.python_version()`.

The existing overview mapper exposes all five fields. Host identity therefore **EXISTS** for MC-6.3, subject to safe redaction and explicit observation wrapping in the future Server response.

### CPU

The existing `SystemService.cpu()` provides physical core count, logical core count, and current utilization through psutil. The existing overview mapper exposes those fields. Load averages are collected separately by `HostTelemetryService._load()` through `os.getloadavg()` and exposed as one-, five-, and fifteen-minute values.

Current CPU utilization, core counts, and load **EXIST**. Historical CPU utilization and load **EXIST** through the existing `/api/history/host` path and host-history mapper.

### Memory and swap

The existing `SystemService.memory()` provides total, used, available, and percentage memory. The overview mapper exposes all four. Swap is collected by `HostTelemetryService._swap()` and includes total, used, percentage, availability, and safe error information.

Memory and swap **EXIST**, including aggregate historical memory/swap fields in existing host history.

### Disk

The existing `SystemService.disk()` calls `psutil.disk_usage("/")` and exposes aggregate root-filesystem total, used, free, and percentage values. These values are present in `/api/overview` and existing host history.

Aggregate root disk **EXISTS**. A filesystem inventory containing mountpoint, filesystem type, total, used, available, and utilization **does not currently exist**. It is an **EXTEND/NEW projection** using the existing psutil boundary, with an allow-list and redaction design required before implementation.

### Network

The existing `HostTelemetryService._network()` provides:

- Number of interfaces from `psutil.net_if_addrs()`.
- Count of `ESTABLISHED` Internet connections from `psutil.net_connections(kind="inet")`.
- Safe unavailable/error state when either operation fails.

Aggregate interface count and established connection count **EXIST**. Per-interface RX/TX counters, interface names/status, and connection-state breakdowns are not currently represented in the host model or mapper. These are **EXTEND/NEW** fields requiring an additive model and bounded mapper projection.

### Health and incidents

The existing Service Pulse API exposes persisted telemetry/MC-3 service observations and freshness. Existing event and incident APIs expose MC-3 event and Incident Room data. The current Dashboard view already presents these sections and does not expose acknowledgement or remediation controls.

Health evidence **EXISTS** as separate existing read APIs and Dashboard sections. A dedicated Server page health summary is an **EXTEND** projection that should compose those existing read services rather than recalculate incidents or create a health database.

### Observation states

The existing telemetry domain has `FreshnessStatus` with `fresh`, `stale`, `unavailable`, and `never_sampled`. MC-6.1 adds the cross-domain `Observation` contract with `unknown` and `error`, while preserving the existing telemetry models.

The future Server façade should use the MC-6.1 contract at its response boundary without rewriting `/api/overview` or changing MC-3/telemetry writer behavior.

## 3. EXISTS / EXTEND / NEW / UNAVAILABLE classification

| Requested Server capability | Classification | Existing evidence | Proposed treatment |
|---|---|---|---|
| Hostname | **EXISTS** | `SystemService.hostname()` and `/api/overview.host.hostname`. | Reuse and expose in Server projection. |
| Operating system | **EXISTS** | `SystemService.os()` and `/api/overview.host.os`. | Reuse. |
| Kernel | **EXISTS** | `SystemService.kernel()` and `/api/overview.host.kernel`. | Reuse. |
| Architecture | **EXISTS** | `SystemService.architecture()` and `/api/overview.host.architecture`. | Reuse. |
| CPU count | **EXISTS** | `SystemService.cpu()` and overview core fields. | Reuse. |
| Uptime | **EXISTS** | `HostTelemetryService._uptime()` and overview uptime object. | Reuse. |
| Current CPU utilization | **EXISTS** | `SystemService.cpu()` and overview CPU object. | Reuse. |
| Current load averages | **EXISTS** | `HostTelemetryService._load()` and overview load object. | Reuse. |
| Historical CPU utilization/load | **EXISTS** | `TelemetryHistoryMapper._host()` and `/api/history/host`. | Reuse existing history API. |
| Total/used/available memory | **EXISTS** | `SystemService.memory()` and overview memory object. | Reuse. |
| Memory utilization | **EXISTS** | `SystemService.memory().percent`. | Reuse. |
| Historical memory | **EXISTS** | Existing host history point. | Reuse. |
| Swap | **EXISTS** | `HostTelemetryService._swap()` and overview swap object. | Reuse. |
| Root filesystem total/used/free/utilization | **EXISTS** | `SystemService.disk()` uses `psutil.disk_usage("/")`. | Reuse. |
| Filesystem inventory | **EXTEND / NEW projection** | Existing psutil dependency, but no partition model or API. | Add bounded filesystem detail only after path/mount redaction design. |
| Interface count | **EXISTS** | `HostTelemetryService._network()`. | Reuse. |
| Established connection count | **EXISTS** | `psutil.net_connections(kind="inet")`. | Reuse. |
| Per-interface RX/TX | **NEW field using existing provider** | No current `net_io_counters()` call or model fields. | Add per-interface counters through a narrowly scoped provider extension. |
| Interface operational state | **EXTEND / NEW field** | Interface addresses exist, but state is not modeled. | Add only if a stable psutil-supported source and safe contract are selected. |
| Connection-state breakdown | **EXTEND** | Existing connection list is already queried but only `ESTABLISHED` is counted. | Add bounded counts by allow-listed state names; do not expose endpoint addresses by default. |
| Host observation freshness | **EXTEND** | Existing service-health freshness and telemetry freshness exist; host response is not wrapped in MC-6.1 `Observation`. | Add to dedicated Server response only. |
| Resource warnings | **EXTEND** | Existing findings/health/event concepts exist, but no Server-specific threshold projection. | Compose existing safe health/finding data; do not create an independent alert engine. |
| Relevant incidents | **EXISTS / EXTEND projection** | Existing MC-3 incident API and Incident Room. | Link or summarize existing incidents by resource; no acknowledgement route. |
| RX/TX historical trend | **UNAVAILABLE in current storage** | Host history stores interface count and established count only. | Defer or design an additive schema migration separately; do not silently infer it. |
| Per-filesystem historical trend | **UNAVAILABLE in current storage** | Host history stores aggregate root disk only. | Defer pending schema and retention design. |
| Remote/public network or Cloudflare account state | **UNAVAILABLE by policy** | Tunnel projection intentionally reports only local cloudflared visibility. | Keep unavailable; no Cloudflare/provider access. |
| Process-level CPU/memory inventory | **UNAVAILABLE for MC-6.3** | No approved process inventory contract exists in current Dashboard. | Defer; avoid arbitrary command lines and sensitive process metadata. |

## 4. Data-flow design

### Recommended first implementation path

The smallest coherent implementation is a dedicated read-only Server façade that reuses the existing host collector and the existing read-only history/incident/service APIs:

```text
SystemService + HostTelemetryService
        ↓
DashboardTelemetryService host collection boundary
        ↓
DashboardServerApi
        ↓
ServerResponseMapper
        ↓
GET /api/server
        ↓
MC-6.3 Server view
```

The Server façade must not instantiate a second psutil collector, query SQLite directly, call Docker, query Cloudflare, invoke systemd lifecycle methods, or execute shell commands.

### Current values versus history

Current Server values should use the existing host collection boundary and remain independent of slow Docker/project refreshes. Existing historical charts should call `/api/history/host`, which already uses the read-only telemetry repository boundary. The Server view must not create a new database or duplicate the history repository.

### Health composition

Server health should be a composed read projection:

```text
Host observation state
+ existing Service Pulse observation state
+ existing MC-3 incident/event read data
+ existing safe finding/resource warning data
        ↓
Server health summary
```

The composition must have isolated unavailable states. A failure to read incidents must not convert a healthy host sample into an unavailable host sample. A failure in one optional field must be represented in that field’s safe error/availability envelope.

## 5. Proposed API contract

### Route

Proposed additive route:

```text
GET /api/server
```

The existing `GET /api/overview` route remains unchanged for backward compatibility. The first implementation should not add query parameters that accept arbitrary filesystem paths, interface names, unit names, shell fragments, or provider identifiers.

### Response shape

The following is a design contract, not an implementation instruction for this phase:

```json
{
  "available": true,
  "status": "ok",
  "observation": {
    "transport_ok": true,
    "available": true,
    "state": "fresh",
    "observed_at": "2026-08-18T12:00:00+00:00",
    "age_seconds": 0,
    "max_age_seconds": 45,
    "error": null
  },
  "identity": {
    "hostname": "host",
    "os": "Linux",
    "kernel": "...",
    "architecture": "x86_64",
    "python": "3.12"
  },
  "uptime": {
    "seconds": 12345,
    "label": "0d 3h 25m"
  },
  "cpu": {
    "usage_percent": 12.5,
    "physical_cores": 4,
    "logical_cores": 8,
    "load": {"one": 0.42, "five": 0.38, "fifteen": 0.31}
  },
  "memory": {
    "total_gb": 16.0,
    "used_gb": 5.0,
    "available_gb": 11.0,
    "percent": 31.2
  },
  "swap": {
    "available": true,
    "total_gb": 2.0,
    "used_gb": 0.1,
    "percent": 5.0,
    "error": null
  },
  "disk": {
    "root": {"path": "/", "total_gb": 100.0, "used_gb": 40.0, "free_gb": 60.0, "percent": 40.0},
    "filesystems": [],
    "error": null
  },
  "network": {
    "available": true,
    "interfaces": [],
    "established": 3,
    "states": {"ESTABLISHED": 3},
    "error": null
  },
  "health": {
    "state": "fresh",
    "service_pulse": {"available": true, "status": "healthy"},
    "incidents": {"available": true, "open": 0},
    "warnings": []
  }
}
```

### Contract rules

1. The route is GET-only.
2. The response uses safe scalar values, bounded arrays, explicit availability, and safe error messages.
3. The route does not expose raw exception text, SQL, environment values, credentials, provider payloads, connection endpoint addresses, arbitrary mount inventories, or command lines.
4. `identity`, CPU, memory, root disk, and current network aggregate fields should preserve the established overview values where the same sample is used.
5. The `observation` envelope distinguishes transport success, data availability, freshness, and semantic error. A transport-successful semantic error is `error`, not `unavailable`.
6. Missing optional fields do not erase valid fields from the same response.
7. The route must not call a write-capable repository constructor. If history or persisted service state is consulted, it must use the existing explicit `read_only=True` boundary.
8. No query parameter may select an arbitrary path, interface, process, unit, provider, or command.

## 6. Observation-state behavior

The Server response should use the MC-6.1 `Observation` contract at the façade boundary.

| Situation | `transport_ok` | `available` | State | UI behavior |
|---|---:|---:|---|---|
| Current host sample collected successfully | `true` | `true` | `fresh` | Show values and fresh badge. |
| Cached/persisted host observation older than threshold | `true` | `true` | `stale` | Show values with stale badge and sample age; never present as current. |
| Host provider fails while a semantic error is known | `true` or `false` | `false` | `error` | Show an error state with safe explanation. |
| Provider responds but no usable host observation exists | `true` | `false` | `unavailable` | Show unavailable state; do not substitute zeros as current values. |
| No prior sample exists in a future cached mode | `true` | `false` | `never_sampled` | Show a clear never-sampled empty state. |
| State cannot be determined safely | `true` | `false` | `unknown` | Show indeterminate state and avoid inference. |

The current direct host collector does not maintain a persisted host snapshot separate from the regular telemetry path. Therefore, the first dedicated Server implementation will naturally produce `fresh` for a successful direct read and `error`/`unavailable` for failures. `stale` and `never_sampled` must be exercised by contract fixtures and any future cached observation path, not simulated as current data.

## 7. Frontend Server page structure

The MC-6.3 Server page should be a read-only view inside the existing vanilla shell. It must use `mission-control-state.js` for state classes and the existing `MissionControlScheduler` for periodic loading.

### Proposed layout

| Section | Content | Data source |
|---|---|---|
| Identity card | Hostname, OS, kernel, architecture, uptime, observation age. | `/api/server`. |
| CPU card | Current usage, physical/logical cores, one/five/fifteen-minute load. | `/api/server`. |
| Memory card | Total, used, available, utilization, swap. | `/api/server`. |
| Disk card | Root filesystem totals and safe filesystem rows if approved. | `/api/server`. |
| Network card | Interface count, connection-state summary, safe RX/TX rows if approved. | `/api/server`. |
| Health card | Observation state, Service Pulse summary, open incident count, resource warnings. | `/api/server` composed from existing read services. |
| History card | CPU, load, memory, disk trends from existing `/api/history/host`. | Existing history route. |

The page should not duplicate the Dashboard renderer’s data logic. Shared state presentation and safe loading/error/empty components should be reused. The Dashboard can continue to show its current summary cards while the Server page becomes the detailed host surface.

### Empty and unavailable behavior

The page must distinguish loading, fresh, stale, unavailable, never-sampled, unknown, and error states. It must not display zero-valued fallback data as if it were a valid current sample. Optional disk/network detail sections may show “not available from current telemetry” when the existing provider does not supply the requested field.

## 8. Polling and freshness strategy

The current Dashboard scheduler uses:

- Overview: 15 seconds.
- Service Pulse: 15 seconds.
- Events: 15 seconds.
- Incidents: 30 seconds.
- Notifications: 30 seconds.
- History: 60 seconds/manual.

The first Server page should use a **30-second Server resource cadence** unless a performance measurement demonstrates that the host-only read can safely run at 15 seconds and the product requires it. Host data is low-cost relative to Docker resource collection, but the contract should still avoid duplicate calls when the Dashboard is visible.

Preferred future optimization:

- Keep one scheduler resource for the Server page.
- Pause hidden views using the existing visibility handling.
- Prevent overlap and apply bounded retry/backoff through `MissionControlScheduler`.
- Avoid polling the Server route when the Dashboard’s overview already supplies the same fresh host sample, unless a shared in-memory snapshot boundary is designed and tested.
- Do not use SSE, WebSockets, or a new worker in MC-6.3.

Historical queries remain manual/60-second and bounded by the existing history service limits.

## 9. Security and read-only analysis

### Application boundary

The host provider calls are read-only observations through psutil, `platform`, `socket`, and `os.getloadavg()`. MC-6.3 must not introduce shell execution, arbitrary command construction, systemd lifecycle calls, Docker lifecycle methods, Git updates, or Cloudflare/provider calls.

### Database boundary

The current overview reads persisted Docker resource samples through `SQLiteHistoryRepository(..., read_only=True)` when telemetry is enabled. That boundary must remain explicit. The future Server route should not need a database for current host values. If it reads historical data, it must reuse the existing history façade and read-only repository path.

The following invariants remain mandatory:

- SQLite `mode=ro`.
- `PRAGMA query_only=ON`.
- Filesystem write-denial boundary.
- No schema initialization or migration from the dashboard.
- No database, WAL, or SHM mutation.
- No second telemetry/event/notification database.

### Output safety

The Server response must not expose:

- Environment variables or credentials.
- Raw exception/traceback details.
- Provider tokens or destinations.
- Full connection endpoint addresses by default.
- Arbitrary mount paths beyond an approved safe projection.
- Process command lines or process environments.
- Cloudflare account state.

Filesystem and interface names should be treated as potentially sensitive. The initial implementation should return only an allow-listed projection, with configurable redaction if required.

### Deployment boundary

MC-6.3 remains loopback-only and compatible with the validated user-level systemd hardening:

```text
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadOnlyPaths=...
RestrictSUIDSGID=true
CapabilityBoundingSet= absent
```

No public ingress, authentication, Cloudflare change, credential configuration, notification activation, or service topology change is part of MC-6.3.

## 10. Testing strategy

### Provider and service tests

- Fake `SystemService` returns deterministic identity, CPU, memory, and disk values.
- Fake psutil module covers load, uptime, interface count, connection states, and provider failures.
- Test that Docker/project failures do not affect host collection.
- Test that optional network/filesystem detail failures remain isolated.
- Test no shell, systemd lifecycle, Docker lifecycle, Git mutation, or provider calls occur.

### Contract and mapper tests

- Complete `/api/server` shape with all currently supported fields.
- Existing `/api/overview` contract remains byte/shape compatible where asserted.
- Fresh, stale, unavailable, never-sampled, unknown, and semantic-error states.
- Safe redaction of paths, endpoints, exception details, and secret-like keys/values.
- Partial provider failure preserves available sibling fields.
- Bounded filesystem/interface arrays.
- No arbitrary path/interface query parameters.

### API tests

- `GET /api/server` returns 200 with a fresh fixture.
- Provider failure returns safe error envelope.
- Optional detail failure does not fail the whole route.
- No POST/PUT/PATCH/DELETE Server route exists.
- Existing history, incidents, services, and overview routes remain unchanged.
- No database writes occur during API construction or GET requests.

### Frontend tests

- `#/server` is present and selected correctly.
- Dashboard remains the default/unknown-route fallback.
- Server page renders identity, CPU, memory, disk, network, health, and history sections from a safe fixture.
- Fresh/stale/unavailable/never-sampled/unknown/error states render distinct labels.
- No zero fallback is presented as current data.
- Scheduler registers one Server resource at the approved cadence and does not overlap/manual-refresh storm.
- Responsive layout remains readable at desktop/tablet/mobile sizes.
- No action controls, mutation methods, or secret-like fixture fields appear.

### Read-only regression tests

Reuse the existing active-WAL and filesystem-boundary tests. A dedicated Server GET test must prove that constructing the real dashboard with a temporary active-WAL database and issuing Server/overview/history GETs leaves the database, WAL, SHM, metadata, schema, and sidecars unchanged.

## 11. Exact future implementation file plan

This is a proposed file plan only; no files are changed by this report.

| File | Proposed change | Classification |
|---|---|---|
| `src/aipm/services/telemetry/host.py` | Extend host collection only for approved filesystem/network detail, preferably through injected provider helpers. | EXTEND. |
| `src/aipm/models/telemetry.py` | Add narrowly scoped optional host network/filesystem detail models if existing models cannot represent the contract. | EXTEND. |
| `src/aipm/models/mission_control.py` | Reuse existing `Observation`; do not create another freshness enum. | EXISTS. |
| `src/aipm/services/telemetry/dashboard.py` | Add a host/server snapshot boundary only if it prevents duplicate collection and preserves fast path behavior. | EXTEND. |
| `src/aipm/capabilities/dashboard/server_api.py` | New read-only façade for the dedicated Server response. | NEW. |
| `src/aipm/mappers/server.py` | New safe mapper for the additive `/api/server` contract; do not alter the overview mapper’s existing shape. | NEW. |
| `src/aipm/dashboard/server.py` | Register only `GET /api/server` and dependency wiring. | EXTEND. |
| `src/aipm/dashboard/static/index.html` | Replace the MC-6.2 Server placeholder with the approved Server view. | EXTEND. |
| `src/aipm/dashboard/static/mission-control-shell.js` | Keep route; add Server view activation only if needed. | EXTEND. |
| `src/aipm/dashboard/static/mission-control-state.js` | Reuse current state classes; extend only for Server-specific safe presentation. | EXTEND. |
| `src/aipm/dashboard/static/mission-control-scheduler.js` | Register the Server resource with one bounded cadence. | EXTEND. |
| `tests/test_server_api.py` | Provider, façade, mapper, error, bounds, redaction, and GET-only contract tests. | NEW. |
| `tests/test_server_frontend.py` | Server view, state, routing, cadence, responsive and no-action fixture tests. | NEW. |
| `tests/test_dashboard.py` | Add only compatibility assertions for the new additive route if required. | EXTEND. |
| `tests/test_telemetry.py` | Extend injected host/provider tests for new optional fields. | EXTEND. |

No database migration, new repository, worker, systemd unit, production template, or deployment script should be required for the first current-value Server view.

## 12. Explicit non-goals

MC-6.3 does not implement:

- Systemd observation API or systemd page details.
- Logs API or log viewer.
- Docker lifecycle operations or Docker exec.
- Git updates, Compose mutation, or project remediation.
- Incident acknowledgement or remediation.
- Notification activation, sending, testing, or provider configuration.
- Settings mutation.
- Authentication, public ingress, Cloudflare changes, or credentials.
- SSE or WebSockets.
- AI Agent execution or action planning.
- Shell/command execution.
- Process inventory or arbitrary command-line disclosure.
- New telemetry, event, incident, notification, or history database.
- New background worker or production service.
- Live VPS deployment or systemd runtime changes.

## 13. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Duplicating host collection beside `DashboardTelemetryService` | Inconsistent values and extra psutil cost. | Reuse the existing host service and add a shared snapshot boundary only if measured necessary. |
| Changing `/api/overview` while adding Server detail | Existing MC-5 clients/UI regress. | Add `/api/server`; leave overview mapper and route shape unchanged. |
| Treating missing data as zero | Stale/unavailable host state appears healthy. | Use MC-6.1 `Observation` states and explicit optional-field envelopes. |
| Exposing mount paths or interface details | Privacy/security leakage. | Allow-list, redact, bound, and test all detail projections. |
| RX/TX counters are mistaken for historical data | False trend interpretation. | Mark current-only until storage/retention is separately designed. |
| Network connection enumeration becomes expensive or sensitive | Latency and endpoint disclosure. | Aggregate states by default; avoid addresses; bound calls and arrays. |
| Dashboard and Server route poll the same host independently | Duplicate load and inconsistent timestamps. | Use a shared in-process snapshot or explicit cadence policy after measurement. |
| SQLite read-only boundary regresses | Live database/WAL/SHM mutation. | Reuse existing read-only façade/repository and active-WAL fingerprint tests. |
| Host provider partially fails | Entire Server page becomes unavailable. | Isolate identity, CPU, memory, disk, network, and health sub-observations. |
| Scope expands into Systemd, Logs, or actions | Safety and delivery risk. | Keep those capabilities as placeholders and enforce route/static mutation scans. |

## 14. Recommended implementation sequence

1. **Contract review:** Approve the additive `/api/server` shape and decide whether filesystem and RX/TX detail are required in the first implementation or remain explicitly unavailable.
2. **Reuse-first provider extension:** Add only injected host provider methods needed for approved missing fields. Preserve `HostTelemetryService.snapshot()` and fast-path behavior.
3. **Typed model extension:** Add optional filesystem/interface detail models only if the contract cannot use existing types. Keep `Observation` as the only cross-domain state contract.
4. **Server façade and mapper:** Implement `DashboardServerApi`/equivalent and `ServerResponseMapper` without changing `/api/overview`.
5. **API route:** Add only `GET /api/server`; enforce safe bounds and no arbitrary selectors.
6. **Focused tests:** Cover provider isolation, mapping, observation states, redaction, GET-only behavior, and active-WAL read-only invariants.
7. **Frontend view:** Replace only the Server placeholder, use the shared state/scheduler modules, and preserve the existing shell and visual language.
8. **Browser and responsive acceptance:** Verify desktop, tablet, and mobile layouts, fresh/stale/unavailable/never-sampled/error rendering, and no action controls.
9. **Regression gate:** Run MC-6.1, MC-6.2, MC-5, full pytest, JavaScript syntax, compilation, diff, mutation-route, secret-scan, and database immutability checks.
10. **Stop for review:** Do not begin MC-6.4 until MC-6.3 is separately reviewed and approved.

## Final design state

```text
MC6.3_DESIGN=COMPLETE
MC6.3_IMPLEMENTATION_STARTED=NO
FILES_CREATED=1 design report
FILES_MODIFIED=0 source/test/production files
PRODUCTION_CHANGES=NONE
RUNTIME_CHANGES=NONE
DATABASE_CHANGES=NONE
SYSTEMD_CHANGES=NONE
DOCKER_CLOUDFLARE_CREDENTIAL_PROVIDER_CHANGES=NONE
MC6.4_STARTED=NO
```
