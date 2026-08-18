# MC-6.4 Design — Server Capability Closure and Roadmap Reconciliation

## 1. Status and decision

This document is a **design and inspection report only**. It does not authorize or perform MC-6.4 implementation, deployment, systemd changes, live-database access, Docker operations, Cloudflare changes, credential access, provider access, notification activation, public ingress, or MC-6.5 work.

The authoritative repository inspected for this report is:

```text
HEAD        = 456af606d7142a2b39a09624218d2a960ba2228f
origin/main = 456af606d7142a2b39a09624218d2a960ba2228f
```

The requested checkpoint `1926197082e50d751d5b5227f485aa28f5d67f6d` is the parent MC-6.3 implementation commit. The subsequent static-asset correction is present in commit `456af606d7142a2b39a09624218d2a960ba2228f`, which is the current repository baseline and contains the reviewed production-compatible module URLs.

The roadmap defines MC-6.4 as the **Server detail and capacity façade/API** milestone. That objective has already been delivered by the current MC-6.3 implementation and its static-asset follow-up. Therefore, the correct MC-6.4 decision is **roadmap reconciliation and acceptance closure, not another Server implementation**. Reimplementing the Server façade would duplicate collectors, APIs, UI, tests, and risk the validated read-only boundary.

> **Decision:** Treat the original MC-6.4 feature objective as already satisfied by the existing Server capability. Do not begin a second MC-6.4 implementation. Any remaining work belongs to acceptance evidence, documentation correction, or a separately approved next milestone.

## 2. Current MC-6 state after MC-6.3

MC-6.1 established the shared `Observation` contract, bounded query helpers, secret-safe payload scanning, vanilla state normalization, and the centralized polling scheduler. MC-6.2 established the persistent vanilla shell, hash navigation, selected-page state, responsive sidebar behavior, and safe placeholders. MC-6.3 replaced the Server placeholder with a functional read-only Server page, a dedicated Server façade, and an additive `GET /api/server` route. The follow-up commit corrected all frontend module URLs to use the existing `/static` mount.

The current delivered Server path is:

```text
SystemService + psutil
        ↓
HostTelemetryService.server_snapshot()
        ↓
DashboardServerApi.server()
        ↓
ServerResponseMapper.to_response()
        ↓
GET /api/server
        ↓
MC-6.3 Server view inside the vanilla shell
```

The Server page also reuses the existing `GET /api/history/host` route for aggregate host history, while its health section composes the existing Service Pulse and MC-3 incident read façades. The dashboard remains loopback-only and uses the existing static mount at `/static`.

### Delivered Server capabilities

The following roadmap requirements are already present in the inspected repository:

| Roadmap requirement | Current state | Evidence |
|---|---|---|
| Typed Server read façade | **EXISTS** | `DashboardServerApi` in `src/aipm/capabilities/dashboard/server_api.py`. |
| Additive Server API | **EXISTS** | `GET /api/server` in `src/aipm/dashboard/server.py`. |
| Host identity | **EXISTS** | `SystemService` values mapped by `ServerResponseMapper`. |
| CPU topology and utilization | **EXISTS** | Existing host snapshot and Server mapper fields. |
| Load averages | **EXISTS** | Existing `HostTelemetryService` load collection. |
| Memory and swap | **EXISTS** | Existing host snapshot and mapper projections. |
| Root disk capacity | **EXISTS** | Existing `SystemService.disk()` projection. |
| Bounded filesystem detail | **EXISTS** | Allow-listed mounts and bounded `FilesystemDetail` values. |
| Bounded interface detail | **EXISTS** | Safe names, operational state, RX/TX counters, and bounded count. |
| Connection-state summary | **EXISTS** | Aggregate state counts without endpoint addresses. |
| Explicit observation state | **EXISTS** | MC-6.1 `Observation` contract at the Server façade boundary. |
| Service Pulse and incident summary | **EXISTS** | Existing read façades composed by `DashboardServerApi`. |
| Functional Server page | **EXISTS** | Server view in `index.html` with responsive sections. |
| Server polling | **EXISTS** | Central scheduler registration at 30 seconds. |
| Host-history reuse | **EXISTS** | Existing bounded `GET /api/history/host` path at 60 seconds/manual cadence. |
| Static module routing | **EXISTS** | Frontend imports resolve through `/static/*.js`; fixed in commit `456af60`. |

## 3. Exact MC-6.4 objective and roadmap drift

The implementation plan states that MC-6.4 should “add the smallest typed server read façade that complements existing host telemetry,” using `SystemService`, host telemetry models, psutil-backed measurements, configuration/version sources, and only the missing concepts of host identity, CPU topology, filesystem summaries, and safe capacity state.[1]

The actual MC-6.3 implementation already follows that design. The current `HostTelemetryService` reuses the existing `SystemService` and psutil boundary, adds bounded filesystem/interface/connection projections, and preserves the original `snapshot()` behavior. `DashboardServerApi` isolates host, health, and incident failures. `ServerResponseMapper` emits a bounded safe contract without arbitrary selectors or raw exception strings. The FastAPI adapter adds only the GET route, and the frontend consumes it through the existing shell and scheduler.

This produces a roadmap mismatch:

| Planned MC-6.4 item | Actual current state | Decision |
|---|---|---|
| Add Server façade/API | Already implemented under MC-6.3 | Mark complete; do not duplicate. |
| Add filesystem summaries | Already implemented with mount allow-list and item cap | Mark complete; retain current-only semantics. |
| Add network/interface detail | Already implemented with safe names and bounded counters | Mark complete; retain endpoint redaction. |
| Add Server page | Already implemented | Mark complete; preserve current shell. |
| Add Server freshness/error behavior | Already implemented via MC-6.1 `Observation` | Mark complete; preserve semantic distinctions. |
| Add Server history | Existing host history reused | Mark complete for aggregate fields; do not fabricate unavailable history. |
| Add production deployment | Separate deployment gate, not MC-6.4 implementation | Remains approval-gated and outside this design task. |

The remaining limitations are not a reason to reimplement MC-6.4. Resource-warning projection is explicitly deferred in the current Server response, while RX/TX and per-filesystem historical trends are not persisted by the existing history schema. They must remain explicitly unavailable or deferred rather than being fabricated. A separate future schema/design decision would be required before storing those histories.

## 4. EXISTS / EXTEND / NEW / UNAVAILABLE classification

The classifications below describe the current repository after MC-6.3, not an invitation to expand MC-6.4 implementation scope.

| Capability | Classification | Current treatment |
|---|---|---|
| Dashboard shell and hash navigation | **EXISTS** | MC-6.2 shell and router are stable. |
| Server identity, CPU, load, memory, swap, uptime | **EXISTS** | Reused from the existing host boundary. |
| Root disk capacity | **EXISTS** | Reused from `SystemService`. |
| Allow-listed filesystem capacity | **EXISTS** | Bounded `FilesystemDetail` projection. |
| Interface name/state/RX/TX | **EXISTS** | Bounded `NetworkInterfaceDetail` projection; no addresses. |
| Connection-state counts | **EXISTS** | Safe aggregate state projection; no endpoints. |
| Service Pulse composition | **EXISTS** | Existing dashboard service-health façade. |
| MC-3 open-incident summary | **EXISTS** | Existing read-only incidents façade. |
| Aggregate CPU/memory/disk/load history | **EXISTS** | Existing `/api/history/host` route and read-only repository. |
| Server-page responsive layout | **EXISTS** | Existing vanilla CSS and Server sections. |
| Server current observation freshness | **EXISTS** | MC-6.1 `Observation` at the Server response boundary. |
| Cached stale Server sample | **EXTEND** | Not a separate persisted Server cache; only contract semantics and future cached modes are defined. Do not simulate stale current data. |
| Resource warning projection | **EXTEND** | Current response explicitly reports it as unavailable; future work must reuse existing findings/health concepts. |
| RX/TX historical trend | **UNAVAILABLE** | Current history schema does not persist interface counters. |
| Per-filesystem historical trend | **UNAVAILABLE** | Current history schema stores aggregate root disk, not filesystem rows. |
| Process inventory and command lines | **UNAVAILABLE by policy** | No approved safe contract; remain excluded. |
| Remote Cloudflare/account network state | **UNAVAILABLE by policy** | Local tunnel visibility only; no Cloudflare API access. |
| Systemd inventory | **NEW, deferred** | Belongs to the later Systemd milestone, not MC-6.4. |
| Logs | **NEW, deferred** | Belongs to the later Logs milestone, not MC-6.4. |
| Docker detail | **EXTEND, deferred** | Belongs to MC-6.5 in the roadmap. |
| Project detail | **EXTEND, deferred** | Belongs to MC-6.6 in the roadmap. |
| Authentication/public ingress | **FUTURE** | Separate security and deployment gate. |
| Actions/remediation | **FUTURE** | Separate authorization, approval, audit, idempotency, and rollback control plane. |

## 5. Existing code, data, and services to reuse

No new collector, repository, database, worker, provider, or deployment topology is justified for MC-6.4. The delivered Server capability correctly reuses:

| Layer | Authoritative component | Required preservation rule |
|---|---|---|
| Host composition | `SystemService` | Continue using standard-library/psutil read paths; do not create a second system collector. |
| Host telemetry | `HostTelemetryService` | Preserve the established `snapshot()` and fast/slow telemetry split. |
| Server façade | `DashboardServerApi` | Keep orchestration read-only and failure-isolated. |
| Server mapper | `ServerResponseMapper` | Keep safe allow-listed serialization; never fall back to arbitrary exception text. |
| Observation contract | `aipm.models.mission_control.Observation` | Preserve transport, availability, freshness, and semantic-error distinctions. |
| History | `DashboardHistoryApi` and existing history repository | Use explicit read-only access; do not add a Server history database. |
| Service health | `DashboardServiceHealthApi` | Compose current telemetry/MC-3 service observations without recalculation. |
| Incidents | `DashboardIncidentsApi` | Summarize open incidents only; do not expose acknowledgement or remediation. |
| HTTP adapter | `aipm.dashboard.server.create_app()` | Keep `/api/overview` unchanged and `/api/server` GET-only. |
| Frontend shell | `mission-control-shell.js` | Keep hash routing and Server selection stable. |
| Frontend state | `mission-control-state.js` | Reuse state classes and explicit unavailable/error rendering. |
| Frontend scheduler | `mission-control-scheduler.js` | Keep one 30-second Server resource and one 60-second/manual history resource. |
| Static serving | FastAPI `/static` mount | Keep module URLs rooted at `/static`; do not duplicate assets or change topology. |

## 6. Proposed architecture and data flow

Because the Server objective already exists, the proposed MC-6.4 architecture is a **closure baseline** rather than a new implementation. If an acceptance hardening pass is separately authorized, it must remain within the current boundaries:

```text
SystemService + psutil
        │
        ▼
HostTelemetryService.server_snapshot()
        │
        ├── existing HostSnapshot
        ├── bounded filesystem details
        ├── bounded interface details
        └── bounded connection-state counts
        │
        ▼
DashboardServerApi.server()
        │
        ├── Observation envelope
        ├── ServiceHealth read projection
        └── MC-3 incident read projection
        │
        ▼
ServerResponseMapper.to_response()
        │
        ▼
GET /api/server
        │
        ├── Server page at #/server
        └── MissionControlScheduler: 30-second Server refresh

GET /api/history/host
        │
        ▼
Server Host History: 60-second/manual bounded refresh
```

The route must not instantiate a write-capable repository, access SQLite for current host values, call Docker, query Cloudflare, invoke systemd lifecycle operations, run Git commands, or execute shell commands. Optional health and incident failures must remain isolated from valid host metrics.

## 7. Backend and API contract

The current additive route is:

```text
GET /api/server
```

Its top-level contract is bounded and safe:

```text
available
status
error
observation
identity
uptime
cpu
memory
swap
disk
network
health
```

The response distinguishes transport success from semantic availability and freshness. An explicit `ObservationError` is serialized as a safe typed message and never confused with ordinary unavailability. Unexpected exception objects are not serialized.

The route remains GET-only. It accepts no arbitrary filesystem, interface, unit, process, provider, command, or path selector. The current response includes bounded filesystem and interface arrays, safe scalar values, aggregate connection states, and safe optional-detail errors. It omits addresses, endpoints, credentials, environments, full command lines, raw provider payloads, and private exception details.

`GET /api/overview` remains the backward-compatible summary route. MC-6.4 must not rewrite its response shape or move existing host collection into a second path that produces inconsistent timestamps. Any later shared in-process snapshot optimization would require separate measurement and tests; it is not part of the current design closure.

## 8. Domain contracts and mapper/facade/service boundaries

The delivered domain boundary uses `ServerHostSnapshot`, `FilesystemDetail`, and `NetworkInterfaceDetail` as optional typed projections over the existing `HostSnapshot`. The MC-6.1 `Observation` contract remains the only cross-domain state contract. No second freshness enum should be introduced.

The service boundary is intentionally narrow. `HostTelemetryService.server_snapshot()` calls the existing host sample and then performs bounded optional detail reads. Its allow-lists cap filesystem mounts, interface count, and interface-name shape. `DashboardServerApi.server()` wraps host results, composes Service Pulse and incidents, and isolates each failure. `ServerResponseMapper` produces the JSON-safe shape and uses allow-listed typed errors.

If a future acceptance hardening task discovers a contract defect, the smallest permissible changes are limited to tests or safe mapper/contract corrections. New data sources, persistence, caching, warning engines, or provider integrations are outside MC-6.4 closure.

## 9. Frontend and responsive behavior

The functional Server page already occupies the `#/server` shell route and contains Identity, CPU & Load, Memory & Swap, Disk, Network, Health, and Host History sections. It uses the existing state classes, empty/error patterns, responsive card layout, and `/static` module paths.

The Server resource refreshes every 30 seconds through the centralized scheduler. Host history refreshes every 60 seconds or after a bounded range change. The Dashboard continues to use its existing 15-second overview/service/event cadence. Navigation must not create duplicate timers or re-fetch the same resource during render.

Responsive acceptance remains required for any future UI alteration: desktop widths preserve the cockpit grid, tablet widths collapse cards without hiding state labels, and mobile widths use the existing drawer and single-column layout. Long hostnames, interface names, unavailable values, error messages, and empty filesystem/interface states must remain readable. No action button, acknowledgement control, notification activation control, or mutation affordance is permitted.

## 10. Polling, freshness, and failure behavior

The current approved strategy is bounded polling, not SSE or WebSockets:

| Resource | Current cadence | Semantics |
|---|---:|---|
| Dashboard overview | 15 seconds | Existing MC-5 contract. |
| Service Pulse | 15 seconds | Existing MC-5 contract. |
| Server current detail | 30 seconds | Host-only bounded observation. |
| Server host history | 60 seconds/manual | Existing aggregate host history. |

A successful HTTP response with `available=false` remains a semantic unavailable/error state, not a transport failure. A transport failure produces a safe error state. Missing optional detail must not erase valid identity, CPU, memory, disk, or network aggregate fields. The UI must never render zero as a substitute for missing data and must never present a stale or unavailable value as current.

Current direct host reads naturally produce fresh observations on success. Stale and never-sampled states remain contractually supported and must be tested through deterministic fixtures or a separately approved cached-observation design; they must not be fabricated in production responses.

## 11. Security and read-only analysis

The current Server path preserves the MC-5/MC-6 safety architecture:

- No mutation HTTP methods or action endpoints were added.
- Host observations use read-only psutil, platform, socket, os-load, and system-service calls.
- Historical access remains behind existing read-only repositories using SQLite URI `mode=ro` and `PRAGMA query_only=ON` where applicable.
- The filesystem write-denial boundary, active-WAL visibility, and database/WAL/SHM immutability requirements remain unchanged.
- The dashboard remains loopback-only.
- The validated service hardening remains `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadOnlyPaths=...`, and `RestrictSUIDSGID=true`, with `CapabilityBoundingSet=` absent.
- No systemd lifecycle command, Docker lifecycle method, Git mutation, Cloudflare operation, provider delivery, credential access, notification activation, or public ingress was introduced.
- Safe mappers do not serialize raw exception strings, SQL, tracebacks, environment values, destinations, tokens, or raw provider payloads.

A future non-loopback deployment would require authentication, authorization, CSRF protections for writes, audit correlation, rate limits, session controls, and explicit Cloudflare approval. None of those are implied by MC-6.4.

## 12. SQLite and database impact

The current Server route does not require a new database, schema, migration, repository, worker, or writer. Current host values come from read-only system telemetry. Host history reuses the existing telemetry history route and repository boundary.

Any future persistence of per-interface RX/TX or per-filesystem trends is currently **UNAVAILABLE**. It would require an additive schema and retention design owned by the existing telemetry repository, with active-WAL visibility, writer compatibility, migration safety, backup/rollback reasoning, and unchanged database/WAL/SHM fingerprints. Such a migration is explicitly outside this MC-6.4 design closure.

## 13. Systemd, Docker, provider, and credential impact

No new systemd service, unit file, manager reload, enablement, start, stop, restart, or template change is required for the Server capability. The existing loopback dashboard service topology is sufficient.

The Server route does not call Docker and does not change Docker/Compose state. Docker/container detail remains the next roadmap capability after Server closure. No Cloudflare or public-ingress change is required. No credentials, notification provider, channel, worker, or provider environment is accessed or configured. `notifications.enabled` remains governed by the existing disabled production posture and is not affected by Server observations.

## 14. Test and regression strategy

The current repository already contains focused MC-6.3 Server API/frontend coverage, MC-6.1/MC-6.2 coverage, MC-5 regressions, static asset mount tests, full pytest coverage, Python compilation, JavaScript syntax checks, GET-only scans, mutation scans, secret/output safety checks, and active-WAL/read-only repository coverage from earlier milestones.

A final acceptance/closure review should preserve the following evidence:

| Test category | Required assertion |
|---|---|
| API contract | `GET /api/server` succeeds with deterministic fake host data and safe shape. |
| Backward compatibility | `/api/overview`, history, services, events, incidents, and notifications retain existing behavior. |
| Observation semantics | Fresh, stale, unavailable, never-sampled, unknown, and semantic-error states remain distinct. |
| Optional-field isolation | Filesystem, interface, connection, health, or incident failures do not erase valid sibling data. |
| Output safety | Private exception strings, credentials, destinations, endpoints, raw paths, and provider payloads never reach responses or UI fixtures. |
| GET-only posture | No POST/PUT/PATCH/DELETE Server route or action control exists. |
| Static assets | Module URLs resolve through `/static`; root-level module paths remain absent/404. |
| Scheduler | Exactly one Server timer, 30-second cadence, no overlap, bounded retry, cleanup, and no polling storm. |
| Read-only database | Dashboard construction and GET requests leave temporary database, WAL, SHM, metadata, schema, and sidecars unchanged. |
| Scope | No source/runtime/production changes occur during design-only review. |

The design phase itself requires only document validation. No test suite needs to be rerun or modified for this report.

## 15. Deployment and rollback considerations

MC-6.4 does not authorize deployment. The existing production deployment remains a separate approval-gated operation requiring a target-VPS read-only preflight for commit, clean repository, configuration, notification-disabled posture, telemetry/MC-3 state, executable, port, filesystem protection, unit state, and rollback readiness.

If a future Server deployment rollback is explicitly approved, it must be narrow and reversible: restore the previous application/static-asset commit, remove only a Server-specific persistent unit or configuration fragment if one was introduced, verify loopback port closure and health, and confirm telemetry, MC-3, notification, Docker, Cloudflare, credentials, and live SQLite state are unchanged. Ordinary UI rollback must never delete the live database or checkpoint WAL.

## 16. Explicit non-goals

MC-6.4 does not implement or authorize:

- A second Server collector, database, history store, cache, worker, or frontend framework.
- Systemd inventory or unit control; those belong to the later Systemd milestone.
- Docker/container detail; that belongs to MC-6.5.
- Project detail or Git posture expansion; that belongs to MC-6.6.
- Logs or arbitrary file/process inspection; those belong to the later Logs milestone.
- Authentication, public ingress, Cloudflare changes, or credentials.
- Notification activation, provider calls, notification tests, or worker startup.
- Incident acknowledgement, remediation, shell execution, Docker exec, Compose mutation, Git fetch/pull/update, or AI Agent execution.
- RX/TX or per-filesystem historical persistence without a separate schema/retention design.
- SSE, WebSockets, or a new real-time worker.
- Production deployment, systemd runtime mutation, or live SQLite access.

## 17. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Reimplementing an already-delivered Server capability | Close/reconcile MC-6.4 instead of creating duplicate façade, mapper, collector, or page. |
| Roadmap and release labels drift | Record the actual baseline and parent/child commit relationship; use repository state as authority for implementation decisions. |
| Duplicate host collection causes inconsistent timestamps or load | Preserve `HostTelemetryService` and measure any future shared snapshot optimization before changing it. |
| Optional filesystem/interface detail becomes sensitive | Keep mount/name allow-lists, bounds, redaction, and no endpoint/address exposure. |
| Missing historical fields are mistaken for zero | Keep RX/TX and per-filesystem trends explicitly unavailable until storage is designed. |
| Server health duplicates incident/service engines | Compose existing read façades and isolate their unavailable states. |
| Static module routing regresses | Keep absolute `/static/*.js` imports and mounted-asset regression coverage. |
| Read-only boundary regresses during history reads | Reuse explicit `read_only=True` repositories and active-WAL fingerprint tests. |
| Scope expands into Systemd, Logs, Docker, Projects, or actions | Stop at MC-6.4 closure and require a separate milestone approval. |

## 18. Exact files and expected changes

### Design-only task

The only file expected to change for this task is:

```text
docs/MC-6.4_DESIGN.md
```

No source, test, ops, systemd, deployment, configuration, database, Docker, Cloudflare, credential, provider, telemetry, MC-3, notification, or runtime file is expected to change.

### Existing implementation evidence, not proposed edits

The following files already implement the roadmap-defined Server objective and should be preserved rather than recreated:

```text
src/aipm/services/telemetry/host.py
src/aipm/models/server.py
src/aipm/capabilities/dashboard/server_api.py
src/aipm/mappers/server.py
src/aipm/dashboard/server.py
src/aipm/dashboard/static/index.html
src/aipm/dashboard/static/mission-control-state.js
src/aipm/dashboard/static/mission-control-scheduler.js
src/aipm/dashboard/static/mission-control-shell.js
tests/test_mc63_server_api.py
tests/test_mc63_frontend.py
tests/test_mc63_static_assets.py
```

## 19. Recommended implementation and governance sequence

Because the feature objective is already implemented, the recommended sequence is:

1. Record this roadmap reconciliation and mark the original Server objective complete.
2. Preserve the current Server implementation and static-mount fix without source redesign.
3. Retain existing MC-6.3 focused, full-suite, static-asset, output-safety, and read-only evidence.
4. If a closure gate is required, run only an explicitly approved local or target-staging acceptance procedure; do not add functionality during closure.
5. Decide whether to re-label the next product slice as MC-6.5 Docker/container detail or create a separately approved hardening milestone for the remaining Server limitations.
6. Do not begin MC-6.5 implementation until its scope, allowed files, data sources, provider boundaries, and tests are separately approved.

## 20. Stop condition

This design task stops after creation and validation of this document. MC-6.4 implementation is not started, and MC-6.5 is not started.

```text
MC6.4_DESIGN=COMPLETE
MC6.4_IMPLEMENTATION_STARTED=NO
MC6.5_STARTED=NO
PRODUCTION_CHANGES=NONE
RUNTIME_CHANGES=NONE
DATABASE_CHANGES=NONE
SYSTEMD_CHANGES=NONE
DOCKER_CLOUDFLARE_CREDENTIAL_PROVIDER_CHANGES=NONE
NOTIFICATIONS_ACTIVATED=NO
PUBLIC_INGRESS_CHANGED=NO
```

## References

[1]: MC-6_IMPLEMENTATION_PLAN.md "MC-6 implementation plan and milestone map"
[2]: MC-6_ARCHITECTURE.md "MC-6 architecture decisions"
[3]: MC-6_UI_SPECIFICATION.md "MC-6 UI and navigation specification"
[4]: MC-6_API_GAP_ANALYSIS.md "MC-6 API gap analysis"
[5]: MC-6.1_FOUNDATION.md "MC-6.1 shared contracts and UI foundation"
[6]: MC-6.3_DESIGN.md "MC-6.3 Server and Host Intelligence design"
[7]: ../src/aipm/capabilities/dashboard/server_api.py "Current Server read façade"
[8]: ../src/aipm/mappers/server.py "Current safe Server response mapper"
[9]: ../src/aipm/services/telemetry/host.py "Current host telemetry boundary"
[10]: ../src/aipm/dashboard/server.py "Current FastAPI dashboard adapter"
[11]: ../src/aipm/dashboard/static/index.html "Current Mission Control UI"
[12]: ../PRODUCTION_ROADMAP.md "AIPM production roadmap"
[13]: ../PROJECT_STATUS.md "AIPM project status"
