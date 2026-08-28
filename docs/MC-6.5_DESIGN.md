# MC-6.5 Design — Docker and Container Detail

> **Current-state notice — 2026-08-28:** This document is retained as part of the AIPM documentation record. Its historical design or milestone narrative remains valid as historical context, but current completion, publication, deployment, and live-observation claims are superseded by [`docs/CURRENT_STATUS.md`](CURRENT_STATUS.md) and [`docs/LIVE_VPANEL_READONLY_FINDINGS.md`](LIVE_VPANEL_READONLY_FINDINGS.md). The current tracked repository is synchronized at `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`; MC-6.12 operational execution remains blocked, and the incident-reopen workstream remains preserved separately in `stash@{0}`.


## 1. Status and exact roadmap objective

This document is a **design and inspection report only**. It does not authorize or perform MC-6.5 implementation, Docker access, VPS/runtime operations, systemd changes, SQLite access, Cloudflare changes, credential/provider access, notification activation, public ingress, commit, or push.

The repository inspected for this report is currently:

```text
HEAD        = 5be3f881007dd475523a7afc9e504847eb9e71a9
origin/main = 5be3f881007dd475523a7afc9e504847eb9e71a9
```

The roadmap defines MC-6.5 as:

> **Docker/container/project detail façades and APIs.**

Its posture is explicitly **read-only**. The implementation plan further specifies project-grouped container detail, inventory, and bounded resource history using the existing Docker providers and models, with no start, stop, restart, remove, prune, exec, Compose mutation, raw SDK payloads, or unbounded logs.[1]

The current MC-6.4 reconciliation closed the original Server milestone because Server & Host Intelligence was already delivered under MC-6.3. MC-6.5 is therefore **not obsolete**: Docker/container detail remains a distinct, unimplemented roadmap capability. However, the “project” portion must be narrowly interpreted as the minimum safe grouping/linkage required to organize Docker observations; full project detail, Git posture, health, and runtime associations remain assigned to MC-6.6.

> **Decision:** MC-6.5 should implement a new read-only Docker/container detail façade and page, extending existing Docker telemetry and project grouping without duplicating collectors, introducing a second database, or absorbing the later MC-6.6 project-detail milestone.

## 2. Roadmap reconciliation against the current repository

MC-6.1, MC-6.2, MC-6.3, and the MC-6.4 reconciliation are complete. The current application already provides a substantial Docker **summary** through `/api/overview`, but it does not yet provide the page-oriented Docker/container detail and inventory API that MC-6.5 requires.

| Capability | Current repository state | MC-6.5 decision |
|---|---|---|
| Docker summary | **EXISTS** | Preserve `/api/overview` and do not duplicate its summary semantics. |
| Container state rows | **EXISTS** | Reuse existing `DockerTelemetryService`, `DockerMapper`, and container models. |
| Aggregate resource telemetry | **EXISTS** | Reuse the fast/slow split and cached resource samples. |
| Project-grouped Docker view | **EXTEND** | Add stable grouping/linkage using existing labels and project inventory; do not implement full project detail. |
| Container detail façade/API | **NEW** | Add bounded read-only routes and typed response models. |
| Image inventory | **NEW** | Add safe read-only projection over `DockerProvider.images()`. |
| Volume inventory | **NEW** | Add safe read-only projection over `DockerProvider.volumes()`. |
| Network inventory | **NEW** | Add safe read-only projection over `DockerProvider.networks()`. |
| Container inspect detail | **NEW** | Add redacted safe projection over `DockerProvider.inspect()`. |
| Bounded recent logs | **DEFERRED / MC-6.8** | Do not make logs part of MC-6.5; keep a future link only. |
| Container resource history | **EXTEND** | Reuse existing history repository/API; add only bounded detail filters if required. |
| Full project detail | **DEFERRED / MC-6.6** | Do not add Git, health, or project-detail façade beyond grouping metadata. |
| Docker lifecycle controls | **FUTURE** | Never expose start, stop, restart, remove, prune, exec, or Compose mutation. |
| Systemd, Logs, TUI | **DEFERRED** | Remain in later roadmap milestones. |

This reconciliation prevents MC-6.5 from duplicating the completed Server capability or silently pulling MC-6.6 into the Docker milestone.

## 3. EXISTS / EXTEND / NEW / UNAVAILABLE classification

| Product capability | Classification | Design treatment |
|---|---|---|
| Dashboard shell and `#/docker` route | **EXISTS** | Reuse the MC-6.2 vanilla shell and selected-state router. |
| Overview Docker summary | **EXISTS** | Preserve `/api/overview.docker` and its current contract. |
| Container identity/state/health/restart/start time | **EXISTS** | Reuse `ContainerSnapshot`, `DockerMapper.container()`, and existing telemetry. |
| Cached resource samples and freshness | **EXISTS** | Reuse `DockerTelemetryService` cache and `TelemetryFreshness`. |
| Fast state-only Docker path | **EXISTS** | Preserve `fast_snapshot()`; it must not call per-container stats. |
| Aggregate slow resource refresh | **EXISTS** | Preserve one provider-bound `stats_all()` operation and single-flight scheduling. |
| Project grouping | **EXTEND** | Normalize Compose labels/stack metadata into a safe project-group key. |
| Container list/detail API | **NEW** | Add a dedicated read-only capability and mapper. |
| Image inventory | **NEW** | Add bounded safe image metadata projection. |
| Volume inventory | **NEW** | Add bounded safe volume metadata projection, omitting mount contents and sensitive labels. |
| Network inventory | **NEW** | Add bounded safe network metadata projection without arbitrary endpoint detail. |
| Container inspect projection | **NEW** | Allow-list identity, state, health, ports, labels, mounts metadata, and network names; omit raw inspect. |
| Project detail/Git posture/health | **DEFERRED / MC-6.6** | Do not reimplement the Project page or Git analysis. |
| Logs | **UNAVAILABLE in MC-6.5** | Defer to the bounded redacted Logs milestone. |
| Historical per-container resources | **EXTEND** | Reuse existing container-resource history route and repository; add bounded filters only if necessary. |
| Image/volume/network historical trends | **UNAVAILABLE** | No current history schema stores these observations; do not fabricate them. |
| Docker daemon unavailable | **EXISTS as failure state** | Return explicit unavailable/error envelopes; do not crash the whole dashboard. |
| Docker endpoint/account metadata | **UNAVAILABLE by policy** | Do not expose daemon socket paths, credentials, registry secrets, or provider internals. |
| Lifecycle actions | **FUTURE** | Separate control-plane milestone only. |

## 4. Existing components that must be reused

The current repository already has the correct observation boundaries. MC-6.5 must extend them rather than adding direct SDK calls in routes or frontend code.

| Layer | Existing component | Required reuse boundary |
|---|---|---|
| Provider | `src/aipm/providers/docker/provider.py` | Use only `list_containers`, `inspect`, `stats`, `stats_all`, `images`, `volumes`, and `networks`; never call `start`, `stop`, or `restart`. |
| Service | `src/aipm/services/docker/service.py` | Add read-only orchestration methods only if provider access needs composition. Keep lifecycle methods out of the new façade. |
| Telemetry | `src/aipm/services/telemetry/docker.py` | Reuse `fast_snapshot()`, `refresh_resources()`, resource cache, aggregate stats, and freshness/error semantics. |
| Aggregation | `src/aipm/services/telemetry/dashboard.py` | Preserve the fast/slow split and existing overview failure isolation. |
| Models | `src/aipm/models/telemetry.py`, `container.py`, `project.py` | Extend with narrowly scoped detail models only where existing types cannot represent a safe contract. |
| Mapping | `src/aipm/mappers/docker.py` | Reuse identity/resource parsing; add safe detail mappers rather than returning raw SDK objects. |
| Project grouping | `src/aipm/services/project/service.py` and project models | Use local discovery and existing Compose/Git metadata only for grouping/linkage. No fetch, pull, checkout, or update. |
| History | Existing `DashboardHistoryApi` and telemetry history repository | Reuse read-only container/resource history; no second store. |
| HTTP | `src/aipm/dashboard/server.py` | Add only GET routes and preserve all existing routes. |
| Frontend | `index.html`, `mission-control-shell.js`, `mission-control-state.js`, `mission-control-scheduler.js` | Replace only the Docker placeholder; keep `/static` module routing and existing state/scheduler behavior. |
| Safety | `query_bounds.py`, `safety.py`, MC-6.1 Observation | Use existing bounds, redaction, secret scanning, and semantic observation states. |

## 5. Backend architecture and data flow

The recommended MC-6.5 architecture is a dedicated read-only Docker capability composed over the existing provider and telemetry boundaries:

```text
DockerProvider observation methods
        │
        ├── list_containers()
        ├── inspect()
        ├── images()
        ├── volumes()
        └── networks()
        │
        ▼
DockerService read methods
        │
        ├── DockerTelemetryService.fast_snapshot()
        ├── DockerTelemetryService.refresh_resources()
        └── existing project grouping metadata
        │
        ▼
DashboardDockerApi / DockerDetailFacade
        │
        ├── bounded list/detail queries
        ├── provider failure isolation
        ├── project grouping
        └── safe typed observations
        │
        ▼
DockerResponseMapper
        │
        ▼
GET /api/docker/summary
GET /api/docker/containers
GET /api/docker/containers/{id}
GET /api/docker/images
GET /api/docker/volumes
GET /api/docker/networks
        │
        ▼
MC-6.5 Docker page in the existing vanilla shell
```

The façade must never instantiate raw Docker SDK clients, invoke lifecycle methods, parse arbitrary provider payloads in the route adapter, or access the Docker socket outside the existing provider boundary. Each list/detail operation must have an explicit timeout or bounded provider call where the underlying provider supports it.

The existing fast/slow telemetry split remains authoritative. A fast Dashboard refresh may list container state and use cached resource samples. The slow aggregate resource refresh may call exactly one `stats_all()` operation behind the provider boundary. MC-6.5 detail reads must not reintroduce per-container stats calls into the fast path or create a second Docker resource sampler.

## 6. Proposed API contracts and compatibility

The following additive GET routes are proposed. They are design contracts only and are not implemented by this task:

```text
GET /api/docker/summary
GET /api/docker/containers
GET /api/docker/containers/{container_id}
GET /api/docker/images
GET /api/docker/volumes
GET /api/docker/networks
```

The existing `/api/overview` route remains unchanged and continues to serve the summary landing page. `/api/history/containers` and `/api/history/container-resources` remain authoritative for historical container and resource data.

### Common response envelope

Every route should return a bounded safe envelope:

```json
{
  "available": true,
  "status": "ok",
  "error": null,
  "observation": {
    "transport_ok": true,
    "available": true,
    "state": "fresh",
    "observed_at": "2026-08-18T12:00:00+00:00",
    "age_seconds": 0,
    "max_age_seconds": 45,
    "error": null
  },
  "items": [],
  "truncated": false
}
```

The `Observation` state must distinguish `fresh`, `stale`, `unavailable`, `never_sampled`, `unknown`, and `error`. A successful provider call with no usable result is not a transport failure. A provider exception must become a safe typed error, never raw Docker SDK text or an arbitrary exception string.

### Container summary/detail shape

A safe container summary may include:

```text
id_suffix
name
project_key
service_name
image_reference
state
health
restart_count
started_at
ports
resource_stats
resource_freshness
```

The detail route may add bounded safe labels, selected network names, safe mount metadata, and structured state timestamps. It must omit raw inspect output, environment values, command lines, secrets, registry credentials, full IDs where unnecessary, host filesystem contents, and arbitrary socket metadata.

### Inventory shape

Image, volume, and network routes should return bounded safe projections. Examples include image ID suffix, repository/tag display, creation time, size; volume name/driver/scope; and network name/driver/scope/container-count summary. Raw SDK objects, arbitrary labels, mount paths, endpoint addresses, and provider-specific metadata should not be serialized without explicit allow-list review.

All query inputs must be bounded and normalized. Container identifiers should be backend-selected opaque IDs or safe name tokens. The browser must not supply arbitrary Docker object selectors, commands, socket paths, or provider arguments.

## 7. Domain models and contracts

MC-6.5 should introduce only the minimum typed models needed for stable detail responses. Candidate models are:

```text
DockerContainerDetail
DockerResourceObservation
DockerProjectGroup
DockerImageSummary
DockerVolumeSummary
DockerNetworkSummary
DockerInventorySnapshot
```

These models should compose existing `ContainerSnapshot`, `ResourceStats`, `TelemetryFreshness`, `TelemetryError`, `Project`, and MC-6.1 `Observation` rather than create a parallel container or freshness hierarchy.

Every model must be immutable or treated as response data, have bounded collections, and distinguish absent values from zero values. Resource values must preserve the existing `ResourceStats` freshness/error semantics. Container identity should use a stable backend projection and never require exposing a complete internal ID to the browser.

## 8. Services, providers, repositories, façades, and mappers

The implementation should follow this sequence of ownership:

1. `DockerProvider` remains the only Docker SDK/subprocess boundary.
2. `DockerService` exposes read-only provider methods needed by the façade; existing lifecycle methods remain outside the read-only path.
3. `DockerTelemetryService` remains authoritative for container state and aggregate resource samples.
4. A new `DashboardDockerApi` or equivalent façade composes detail queries, grouping metadata, bounds, and failure isolation.
5. A new `DockerResponseMapper` or narrowly extended `mappers/docker.py` converts typed models to safe JSON.
6. The FastAPI adapter registers only additive GET routes.
7. The frontend consumes the façade through `/api/docker/*`; it never imports Docker SDK code or renders raw provider payloads.

No telemetry SQLite repository change should be necessary for current Docker detail. Existing history reads must continue to use the explicit read-only repository boundary. If a future new history field requires schema work, that is a separate migration decision and not part of the first MC-6.5 implementation.

## 9. Frontend information architecture and UX

The existing `#/docker` placeholder should become the functional Docker page inside the current shell. The page should provide:

| Section | Content | Source |
|---|---|---|
| Docker status | Availability, daemon observation state, last observation time, safe error | `/api/docker/summary` |
| Project groups | Grouped container counts and aggregate state | Summary/detail façade plus existing project/Compose labels |
| Containers | Name, service, image, state, health, restart count, resource freshness | `/api/docker/containers` |
| Resource detail | CPU/memory values, sampled time, stale/unavailable state | Existing telemetry/resource cache |
| Container drawer | Safe detail, selected labels, ports, networks, mounts metadata | `/api/docker/containers/{id}` |
| Images | Bounded inventory summary | `/api/docker/images` |
| Volumes | Bounded inventory summary | `/api/docker/volumes` |
| Networks | Bounded inventory summary | `/api/docker/networks` |
| History link | Existing container/resource history | Existing history routes |

The page must clearly state that it is an observation surface. A stopped, unhealthy, or resource-unavailable container is displayed as observed state and may link to an incident or handbook view, but it must not expose lifecycle buttons. Logs remain a future/MC-6.8 surface rather than a raw log drawer in MC-6.5.

The responsive layout should reuse existing cards, tables, badges, drawers, empty states, and error states. Dense container tables should become horizontally scrollable or labeled rows on narrow screens. Long image references and project names must wrap safely. State meaning must not depend on color alone.

## 10. Polling, freshness, and Observation semantics

MC-6.5 must preserve the MC-2.1 fast/slow telemetry split:

| Resource | Proposed cadence | Boundary |
|---|---:|---|
| Docker summary/state | 15 seconds through existing overview-compatible state path | No per-container stats on fast path. |
| Container detail | On page entry and bounded manual refresh | No automatic fan-out across every container. |
| Aggregate resources | Existing slow single-flight refresh | One provider-bound `stats_all()` call with timeout. |
| Container resource history | 60 seconds/manual | Reuse existing history route and read-only repository. |
| Inventory images/volumes/networks | On page entry/manual, with bounded cache if later justified | No continuous high-cost polling. |

The frontend scheduler must register one resource per family, prevent overlap, pause hidden work where safe, and clean up on navigation/page exit. The Docker page must not cause a polling storm by requesting detail for every row at every cadence.

Freshness is domain data, not a client guess. Existing `TelemetryFreshness` remains authoritative for resource samples, while MC-6.1 `Observation` wraps façade-level transport, availability, state, and safe errors. A cached aggregate resource sample may be shown as stale with its sample timestamp; it must never be rendered as current. A stopped container’s resource state should be explicitly unavailable with a safe reason, not zero-valued.

## 11. Security and read-only boundaries

MC-6.5 must preserve all MC-6.1/6.2/6.3 safety invariants:

- Only GET routes are added for Docker observation.
- The new façade calls only Docker observation methods: list, inspect, stats, aggregate stats, images, volumes, networks, and bounded future logs only when separately approved.
- `start`, `stop`, `restart`, `remove`, `prune`, `exec`, Compose mutation, Git mutation, and shell commands are unreachable from the read-only route path.
- The route layer never receives or forwards arbitrary Docker socket, command, path, label, environment, or provider arguments.
- Raw Docker inspect payloads are never serialized.
- Environment variables, secrets, registry credentials, command lines, authorization data, full socket metadata, private host paths, endpoint addresses, and raw provider errors are redacted or omitted.
- Docker failure is isolated to Docker-specific fields; the Dashboard and Server pages remain available where their own sources work.
- The dashboard remains loopback-only and uses the existing user-level systemd hardening and read-only SQLite boundary.
- No notification activation, credential access, Cloudflare change, public ingress, or new worker is introduced.

The provider currently contains both read and lifecycle methods in one class. Static and focused tests must prove that the new façade cannot call lifecycle methods, and fake providers should fail the test if such a call occurs.

## 12. SQLite and database impact

MC-6.5 should not require a new database, repository, schema, migration, writer, or background process. Current Docker state and aggregate resource samples are already represented by existing telemetry services. Historical container/resource data is already served by existing history routes and repositories.

If the implementation needs to read history, it must use the existing dashboard read-only façade and repository constructor with `read_only=True`, SQLite URI `mode=ro`, `PRAGMA query_only=ON`, and the fail-closed filesystem boundary. Dashboard construction and GET requests must not initialize schemas, migrate, commit, checkpoint, change journal/WAL state, or modify database/WAL/SHM sidecars.

Image, volume, network, and inventory history is currently unavailable because no authoritative history schema persists it. MC-6.5 must not fabricate trends or write opaque dashboard snapshots. Any additive schema extension requires a separate approved design covering writer compatibility, retention, active-WAL visibility, fingerprint stability, backup, and rollback.

## 13. Systemd impact

No systemd unit or manager change is required by the Docker detail capability. The existing loopback-only dashboard service remains the deployment boundary. MC-6.5 must not start, stop, restart, enable, disable, reload, or inspect live systemd state as part of implementation or local tests.

If target staging is later approved, it should use a temporary loopback process or separately approved staging unit and preserve the validated properties: `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadOnlyPaths=...`, `RestrictSUIDSGID=true`, and no `CapabilityBoundingSet=`. Docker observation must remain read-only under the same process/service boundary.

## 14. Docker impact

Docker is the primary observed provider for MC-6.5, but no Docker runtime state may be changed by the design or read-only façade. Local tests should use fake provider objects and deterministic snapshots. Optional integration testing may use an isolated disposable Docker fixture only under a separate explicit approval; it must not attach to production Docker or Compose state.

The existing aggregate `stats_all()` subprocess is a provider-bound read operation with a bounded timeout. It must remain behind `DockerProvider` and must not be reimplemented in the route or frontend. Fast telemetry must not call per-container stats. Inventory calls must be bounded and failure-isolated.

## 15. Cloudflare/public-ingress impact

None. MC-6.5 does not alter Cloudflare, cloudflared, tunnel configuration, DNS, public routes, reverse proxies, or authentication. Local tunnel visibility remains the existing local observation only. The dashboard remains loopback-only.

## 16. Credentials and provider impact

No credentials are required or accessed for the design or first read-only implementation. Docker provider errors must be translated into safe typed errors without exposing daemon connection details or environment values. No registry login, provider API call, notification channel, webhook, token, secret, or credential reference is introduced.

## 17. Testing strategy

MC-6.5 requires layered tests before implementation approval:

| Layer | Required coverage |
|---|---|
| Domain models | Stable detail models, identity normalization, bounded arrays, zero-versus-unavailable semantics. |
| Provider boundary | Fake provider proves observation methods are called and lifecycle methods are never called. |
| Docker service | List/inspect/inventory failure isolation and safe error translation. |
| Telemetry | Fast snapshot never calls per-container stats; aggregate refresh is single-flight/bounded; cached resource freshness remains correct. |
| Grouping | Compose/project labels produce stable safe project keys without Git fetch or project mutation. |
| Mapper/output | Raw inspect payloads, environment, secrets, endpoints, command lines, private paths, and provider exception text never appear. |
| API | GET-only routes, bounds, stable envelopes, unavailable Docker, empty inventory, not-found detail, and additive compatibility. |
| Frontend | Docker route selection, grouped table, detail drawer, inventory sections, fresh/stale/unavailable/never-sampled/error states, no action controls, and responsive layout. |
| History | Existing container/resource history remains compatible and read-only; unavailable inventory history is explicit. |
| Safety | Static lifecycle-method scan, provider-call guard, secret scan, mutation-route scan, and no external network/provider tests. |
| Integration | Fake/temporary provider fixtures; no production Docker, SQLite, systemd, Cloudflare, or credentials. |

The existing MC-5, MC-6.1, MC-6.2, and MC-6.3 suites must remain green. The active-WAL repository tests remain mandatory whenever history façades are constructed.

## 18. Deployment and rollback considerations

This design does not authorize deployment. If MC-6.5 is later approved for local implementation, it should use temporary/fake Docker state and loopback-only FastAPI tests. Target staging is a separate approval gate requiring a SHA-verified operator procedure, temporary or isolated Docker/read state, no production Compose mutation, and automatic cleanup.

A future production rollout would be additive: deploy the application commit containing the read-only Docker façade and static page changes through the existing loopback dashboard service. No new worker, database, systemd unit, proxy, Cloudflare rule, credential, or public listener should be required.

Rollback must restore the previous application/static-asset commit or remove only the MC-6.5 read-only page/API code if it was introduced independently. It must not delete containers, images, volumes, networks, databases, WAL/SHM files, or Docker state. After rollback, verify dashboard health, port closure or prior listener state, telemetry/MC-3 continuity, notifications disabled, and unchanged public ingress.

## 19. Explicit non-goals

MC-6.5 does not implement or authorize:

- Docker start, stop, restart, remove, prune, exec, attach, or Compose mutation.
- Git fetch, pull, checkout, stash, reset, clean, project update, or arbitrary project script execution.
- Full project detail, Git posture, health, or runtime associations assigned to MC-6.6.
- General logs or unbounded Docker logs assigned to MC-6.8.
- Systemd observation assigned to MC-6.7.
- New telemetry, database, repository, migration, worker, or scheduler process.
- Cloudflare, public ingress, authentication, credentials, providers, notification activation, or Gate 3.
- Raw Docker SDK payloads, environment values, command lines, endpoint addresses, secrets, or private host paths.
- Image/volume/network historical trends without an approved storage design.
- SSE, WebSockets, AI Agent actions, remediation, or any mutation endpoint.
- MC-6.6 implementation.

## 20. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Calling lifecycle methods through the shared Docker provider | Restrict the new façade protocol to observation methods and use fake-provider guards/static scans. |
| Reintroducing per-container stats on the fast path | Reuse `fast_snapshot()` and cached resources; reserve aggregate `stats_all()` for the bounded slow path. |
| Docker daemon latency blocks the Dashboard | Isolate Docker failures, apply timeouts, and keep host/Server/MC-3 reads independent. |
| Raw inspect data leaks secrets or private paths | Typed allow-list mapper, redaction, bounded fields, and response secret scans. |
| Project grouping silently becomes project management | Use grouping metadata only; defer Git/health/runtime detail to MC-6.6. |
| Inventory polling becomes expensive | Load inventory on page entry/manual refresh with bounded results; do not poll all inventory continuously. |
| Stale resource samples look current | Preserve `TelemetryFreshness`, sample timestamps, and explicit stale/unavailable labels. |
| Docker unavailable breaks all Mission Control | Return Docker-specific unavailable state and preserve other domain responses. |
| History schema is expanded prematurely | Mark inventory history unavailable; reuse current history only; require separate migration design. |
| New API breaks `/api/overview` clients | Add routes and fields additively; do not rewrite existing mapper semantics. |
| UI adds accidental action affordances | Static no-action scans, fixture tests, and explicit read-only banner/empty states. |
| Scope expands into MC-6.6/6.7/6.8 | Require separate approval and stop at MC-6.5 completion. |

## 21. Exact files expected if implementation is later approved

This design task creates no implementation files. A later approved MC-6.5 implementation would be expected to touch only a reviewed subset of the following paths:

```text
src/aipm/models/container.py
src/aipm/models/telemetry.py                 # only if existing types cannot represent detail
src/aipm/models/docker.py                    # possible new typed inventory/detail models
src/aipm/providers/docker/provider.py        # only additive read-method helpers if required
src/aipm/services/docker/service.py          # read-only orchestration only
src/aipm/services/telemetry/docker.py        # only bounded detail/cache integration
src/aipm/services/telemetry/dashboard.py     # only if a shared snapshot boundary is justified
src/aipm/services/project/service.py         # grouping metadata only, if required
src/aipm/mappers/docker.py                   # safe detail/inventory mapping
src/aipm/capabilities/dashboard/docker_api.py # new read-only façade
src/aipm/dashboard/server.py                 # additive GET routes only
src/aipm/dashboard/static/index.html         # replace Docker placeholder only
src/aipm/dashboard/static/mission-control-state.js
src/aipm/dashboard/static/mission-control-scheduler.js
src/aipm/dashboard/static/mission-control-shell.js

tests/test_docker_api.py
tests/test_docker_frontend.py
tests/test_docker_read_only.py
tests/test_telemetry.py
tests/test_dashboard.py                 # compatibility assertions only if required
```

No systemd template, staging harness, database migration, notification file, Docker Compose file, Cloudflare file, credential file, or production configuration file should be required for the first read-only Docker detail implementation.

## 22. Clear implementation sequence

1. **Contract review:** approve the bounded Docker summary/container/detail/inventory shapes and decide the exact safe fields for inspect, images, volumes, networks, and project grouping.
2. **Protocol boundary:** define a read-only Docker observation protocol or façade dependency that cannot expose lifecycle methods to the dashboard.
3. **Model and mapper:** add typed detail/inventory models and safe mappers; prove raw provider payloads and private fields cannot serialize.
4. **Telemetry reuse:** integrate existing fast snapshots, aggregate resource refresh, cache, timeout, and freshness semantics without changing the MC-2.1 split.
5. **Grouping:** add only the minimum stable project/Compose grouping metadata; explicitly defer MC-6.6 project detail.
6. **API:** add additive GET routes with bounded selectors, limits, safe envelopes, and isolated Docker-unavailable behavior.
7. **Focused tests:** cover provider guards, lifecycle exclusion, resource freshness, safe mapping, bounds, output scanning, and route compatibility.
8. **Frontend:** replace only the `#/docker` placeholder; reuse `/static` assets, shell, state classes, scheduler, responsive components, and no-action posture.
9. **Read-only integration tests:** use fake/temporary providers and existing temporary SQLite fixtures for history; prove no database/WAL/SHM mutation.
10. **Regression gate:** run MC-5, MC-6.1, MC-6.2, MC-6.3, full pytest, JavaScript syntax, compilation, diff, secret, mutation, lifecycle, and static-mount scans.
11. **Optional staged acceptance:** require a separate explicit approval for target-VPS staging; keep Docker observation loopback-only and never mutate production Docker.
12. **Stop for review:** stop after MC-6.5 validation and do not begin MC-6.6 automatically.

## 23. Stop condition

This task stops after creation and validation of this design report. MC-6.5 implementation is not started and MC-6.6 is not started.

```text
MC6.5_DESIGN=COMPLETE
MC6.5_IMPLEMENTATION_STARTED=NO
MC6.6_STARTED=NO
PRODUCTION_CHANGES=NONE
RUNTIME_CHANGES=NONE
DATABASE_CHANGES=NONE
SYSTEMD_CHANGES=NONE
DOCKER_RUNTIME_CHANGES=NONE
CLOUDFLARE_CHANGES=NONE
CREDENTIAL_PROVIDER_CHANGES=NONE
NOTIFICATIONS_ACTIVATED=NO
PUBLIC_INGRESS_CHANGED=NO
```

## References

[1]: ../PRODUCTION_ROADMAP.md "AIPM production roadmap"
[2]: MC-6_IMPLEMENTATION_PLAN.md "MC-6 milestone map and implementation plan"
[3]: MC-6_ARCHITECTURE.md "MC-6 architecture decisions"
[4]: MC-6_UI_SPECIFICATION.md "MC-6 UI and navigation specification"
[5]: MC-6_API_GAP_ANALYSIS.md "MC-6 API gap analysis"
[6]: MC-6.1_FOUNDATION.md "MC-6.1 shared contracts and UI foundation"
[7]: MC-6.3_DESIGN.md "MC-6.3 Server design and implementation boundary"
[8]: MC-6.4_DESIGN.md "MC-6.4 Server capability reconciliation"
[9]: ../src/aipm/providers/docker/provider.py "Docker provider boundary"
[10]: ../src/aipm/services/telemetry/docker.py "Docker telemetry fast/slow split"
[11]: ../src/aipm/services/project/service.py "Project discovery and local Git posture"
[12]: ../src/aipm/mappers/dashboard.py "Existing Docker and project overview mapper"
[13]: ../src/aipm/dashboard/static/mission-control-shell.js "Existing Mission Control shell and Docker route"
