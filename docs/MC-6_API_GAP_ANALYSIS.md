# AIPM Mission Control MC-6 API Gap Analysis

## Purpose and contract rules

This analysis compares the current MC-5 API surface with the MC-6 cockpit vision. MC-6.1 through MC-6.8 and MC-6.13 Phases 2/3/4A/4B are now implemented; this document remains the compatibility and gap reference for the completed surface and remaining milestones. The current checkpoint is `af1a10b1f150335df27fda5d915f44e4f14146f4`.

The first MC-6 API release remains read-only and preserves all existing route names and response semantics. The private authenticated `POST /api/advisor/evaluate` boundary is now landed in MC-6.13 Phase 4B. It accepts only bounded caller-supplied input, fails closed when authentication is unavailable or rejected, delegates directly to Phase 4A, and returns the existing `AdvisorResponse`. It is not public and does not collect live observations. Other POST, PUT, PATCH, DELETE, acknowledgement, activation, retry, restart, update, shell, or remediation endpoints remain **FUTURE**. MC-6.13 Phases 2/3/4A remain pure domain/composition layers; Phases 4C–4E remain separate future decisions.

## Current API inventory

| Route | Classification | Current source | MC-6 treatment |
|---|---|---|---|
| `GET /healthz` | EXISTS | FastAPI adapter | Keep as process health only; do not imply telemetry health. |
| `GET /` | EXISTS | Static `index.html` | Keep; evolve shell incrementally. |
| `GET /api/overview` | EXISTS | `DashboardApi` and `DashboardResponseMapper` | Preserve contract; add only safe additive fields. |
| `GET /api/history/host` | EXISTS / EXTEND | `DashboardHistoryApi` and `HistoricalQueryService` | Preserve; add bounded aggregation/comparison later. |
| `GET /api/history/containers` | EXISTS / EXTEND | Existing telemetry history repository/service | Preserve; add project/filter dimensions only additively. |
| `GET /api/history/container-resources` | EXISTS / EXTEND | Existing sparse resource history | Preserve; document freshness and sparse data. |
| `GET /api/history/projects` | EXISTS / EXTEND | Existing history repository/service | Preserve; add comparison windows later. |
| `GET /api/history/tunnel` | EXISTS / EXTEND | Existing history repository/service | Preserve; no Cloudflare account data. |
| `GET /api/events` | EXISTS / EXTEND | `DashboardIncidentsApi` and MC-3 query service | Preserve; add cursor pagination and evidence links later. |
| `GET /api/events/{event_id}` | EXISTS | MC-3 query service | Preserve; safe not-found/unavailable responses. |
| `GET /api/incidents` | EXISTS / EXTEND | MC-3 incident query service | Preserve; add bounded cursor/filter support later. |
| `GET /api/incidents/{incident_id}` | EXISTS | MC-3 incident query service | Preserve; add evidence/timeline expansions later. |
| `GET /api/notifications` | EXISTS / EXTEND | MC-4/4.5 read-only notification repository | Preserve; add safe delivery analytics dimensions. |
| `GET /api/notifications/{notification_id}` | EXISTS | MC-4/4.5 read-only repository | Preserve; never return provider secrets or raw destination data. |
| `GET /api/notification-channels` | EXISTS / EXTEND | Sanitized configuration projection | Preserve; add `notifications_enabled` and safe counts if additive. |
| `GET /api/notification-policies` | EXISTS / EXTEND | Sanitized configuration projection | Preserve; no raw config or secret references. |
| `GET /api/notification-metrics` | EXISTS | MC-4.5 repository metrics | Preserve; add time-window filters only if bounded. |
| `GET /api/services` | EXISTS / EXTEND | Dashboard service-health projection | Preserve freshness semantics; extend to more observed services later. |
| `GET /api/logs` | COMPLETE MC-6.8 | Bounded Logs façade with backend-owned sources and redaction | Preserve GET-only behavior; extend only through reviewed bounded evidence/correlation work. |
| `POST /api/incidents/{id}/acknowledge` | FUTURE / NOT EXPOSED | Underlying façade method exists, HTTP route is not mounted | Keep unavailable in the first MC-6 UI/API. Require separate authorization and audit design before exposure. |

## Contract invariants

Every MC-6 GET response must satisfy these invariants:

1. It returns a bounded JSON structure with explicit `available`, `status`, and safe `error` fields where the resource can be unavailable.
2. It does not expose SQL statements, tracebacks, environment values, secret references, tokens, webhook destinations, authorization headers, raw provider payloads, or unbounded private paths.
3. It distinguishes `fresh`, `stale`, `unavailable`, `never_sampled`, and `unknown` where observation freshness is meaningful.
4. It enforces server-side range, limit, offset/cursor, line, byte, and filter bounds regardless of client validation.
5. It performs no schema creation, migration, commit, checkpoint, filesystem write, provider delivery, notification activation, Docker mutation, systemd mutation, Git fetch, or Cloudflare operation.
6. It remains usable when one source is unavailable; failures are isolated to the affected domain.

## Gap map by product area

### Server and host detail

**Classification: EXTEND existing overview; NEW dedicated detail façade.**

The current `/api/overview` already exposes host CPU, memory, disk, load, uptime, swap, and network summary. MC-6 needs a stable server detail contract rather than continuing to grow one large overview object.

Proposed route:

```text
GET /api/server
GET /api/server/filesystems
```

The first route should return host identity, AIPM version, kernel/OS summary, CPU topology, memory/swap, load, uptime, network counts, and observation metadata. The filesystem route should expose an allow-listed set of mount summaries with capacity, used, free, and percentage values. It must not enumerate arbitrary paths or disclose unnecessary private directory names.

### Docker and containers

**Classification: EXTEND existing Docker telemetry; NEW detail/inventory façades.**

Existing `DockerTelemetryService`, `DockerProvider`, container models, and resource history are authoritative for current state and metrics. The gap is a stable page-oriented API for project-grouped containers and read-only inventory.

Proposed routes:

```text
GET /api/docker/summary
GET /api/docker/containers
GET /api/docker/containers/{container_id}
GET /api/docker/images
GET /api/docker/volumes
GET /api/docker/networks
```

Logs are intentionally separated into the Logs capability. Every route must use bounded provider calls, avoid raw SDK payloads, normalize container identity, and return `available=false` when Docker is unavailable. No lifecycle route is proposed in the read-only release.

### Projects

**Classification: EXTEND existing project inventory; NEW detail façade.**

The current overview includes project inventory through `ProjectService`, Git snapshots, and project telemetry. MC-6 needs a detail route with health summary, Git posture, Compose/runtime association, related containers, latest telemetry, and event references.

Proposed routes:

```text
GET /api/projects
GET /api/projects/{project_id}
GET /api/projects/{project_id}/health
```

The project identifier must be an opaque stable local identifier or a safely encoded project path selected by the backend. The API must not fetch remotes, execute update scripts, mutate Git state, or expose full arbitrary environment files.

### Systemd observation

**Classification: NEW.**

The existing repository contains systemd detection in telemetry/tunnel health but no general read-only systemd inventory façade. MC-6 needs one shared adapter that returns structured observations for an allow-listed unit set.

| Layer | Proposed design |
|---|---|
| Data source | User/system systemd manager through structured, read-only queries such as `systemctl show` and `systemctl list-units`; no arbitrary shell command input. |
| Repository/service layer | New `SystemdObservationService` behind a provider/adapter interface; no SQLite repository is required for current state. Historical unit observations may later use existing telemetry/history storage or a separate approved schema extension. |
| Capability/API layer | New `DashboardSystemdApi` or `SystemdObservationApi` façade with bounded list/detail methods. |
| Mapper/schema | New typed `SystemdUnitSnapshot`, `SystemdUnitDetail`, and safe status/error enums. |
| Frontend consumer | Systemd page, Dashboard service card, incident links, and future TUI systemd panel. |
| Security implications | Read-only query allow-list, no environment/secret output, no mutation verbs, bounded dependency depth, loopback-only access, no user-controlled command arguments. |
| Tests required | Fake adapter state tests, unavailable manager tests, unit filtering/bounds, secret/redaction tests, no-mutation static guard, API contract tests, TUI rendering tests, and integration tests proving no start/stop/reload calls. |

Proposed routes:

```text
GET /api/systemd/units
GET /api/systemd/units/{unit_name}
```

The first version should be limited to known AIPM units and explicitly configured safe units rather than exposing every unit on the host.

### Logs

**Classification: NEW.**

No general bounded log API exists today. Logs are high-risk because they may contain credentials, tokens, command lines, or sensitive application data.

| Layer | Proposed design |
|---|---|
| Data source | Allow-listed journald unit streams and selected AIPM log files only. |
| Repository/service layer | New `ReadOnlyLogService` with a source registry, line/byte/time budgets, redaction pipeline, and cursor support. |
| Capability/API layer | New `DashboardLogsApi` façade. |
| Mapper/schema | New `LogEntry`, `LogPage`, `LogCursor`, and safe redaction metadata. |
| Frontend consumer | Logs page and incident/event detail links. |
| Security implications | No arbitrary path or command input; redact credential patterns; cap output; no browser download of raw logs; avoid full command lines and environment values. |
| Tests required | Source allow-list, byte/line limits, redaction patterns, truncation markers, cursor behavior, unavailable source, injection-safe rendering, no-provider/no-mutation tests. |

Proposed route:

```text
GET /api/logs?source=aipm-dashboard&since=...&limit=200&cursor=...
```

The source identifier is an allow-listed symbolic ID, not a filesystem path or unit string supplied verbatim by the browser.

### Incidents and history

**Classification: EXISTS / EXTEND.**

The current MC-3 query services and read-only repositories remain authoritative. The primary gaps are richer evidence and timeline projections, stable cursor pagination, event/incident cross-links, and chart query aggregation. These should be additive extensions, not new event or incident stores.

Possible additive routes:

```text
GET /api/incidents/{incident_id}/timeline
GET /api/events/{event_id}/evidence
GET /api/history/compare
```

Each route must reuse existing repositories/services, preserve event keys and incident correlation semantics, and remain read-only.

### Notifications and settings

**Classification: EXISTS / EXTEND.**

The current notification APIs already provide audit records, metrics, safe channel metadata, and policy projections. The gap is a safe effective-settings summary and more explicit production posture.

Proposed additive routes:

```text
GET /api/settings/effective
GET /api/settings/posture
GET /api/notifications/summary
```

The settings projection must return booleans, bounded numeric values, safe enum names, counts, and deployment posture. It must never return raw YAML, secret references, environment variable names, destinations, tokens, channel payloads, or credential readiness details that could reveal how to access a provider.

`notifications.enabled` remains false in the current production posture. No MC-6 route enables notifications, creates a channel, sends a test, starts a worker, or exposes credentials.

### Authentication and future actions

**Classification: FUTURE.**

The current loopback-only dashboard is suitable for local access or an operator SSH port-forward. No new authentication API is required for the first read-only local release. Public exposure requires a separate authenticated ingress and authorization design.

Future action routes must not be designed as ordinary extensions to current GET façades. They require:

- Explicit identity and role.
- CSRF protection and request binding.
- Human approval for consequential actions.
- Idempotency keys and action state.
- Precondition checks and concurrency control.
- Audit records with actor, target, plan, approval, and outcome.
- Timeout, cancellation, rollback, and failure semantics.
- Safe output redaction.
- Separate service permissions from the read-only dashboard.

## Current implementation reconciliation

MC-6.1 through MC-6.8 are complete. The implemented additive routes include Server, Docker inventory/detail, Project/Application detail, Systemd unit observation, and bounded `/api/logs`. The Logs contract uses symbolic source IDs, bounded time/line/byte/cursor parameters, fixed local adapters, redaction before mapping, safe source errors, and no raw provider payloads.

The next API work is MC-6.9 design/inspection only: bounded incident/history evidence, cursor pagination, comparisons, timelines, and cross-links built on existing MC-3 and history repositories. MC-6.10 may add sanitized settings/notification posture projections. No new database, schema, worker, notification provider, Docker lifecycle route, Systemd control route, or public-ingress route is authorized by these milestones.

## API versioning and compatibility

The first MC-6 release remains unversioned to preserve current routes, but route response contracts should gain an internal schema version or `contract_version` only if clients can ignore unknown fields. A future breaking redesign should use `/api/v2` rather than silently changing `/api` semantics.

Query parameters must be bounded and normalized consistently. A shared query validation helper should handle ranges, limits, cursors, enum filters, and maximum time windows. It must be used by HTTP and TUI façades.

## API gap priority

| Priority | Gap | Classification | Reason |
|---:|---|---|---|
| P0 | Shared client scheduler and page navigation | EXTEND | Needed to scale the current frontend without polling storms. |
| P0 | Server read façade | EXTEND/NEW | Provides a clean boundary for the Server page. |
| P0 | Docker/project detail façades | EXTEND/NEW | Enables cockpit navigation while reusing providers. |
| P1 | Systemd observation API | NEW | Required for a cPanel/Webmin-style operations cockpit, but must remain read-only. |
| P1 | Bounded log API | COMPLETE MC-6.8 | High-value read-only Logs route delivered with redaction, symbolic sources, fixed adapters, and strict bounds. |
| P1 | Settings posture API | EXTEND | Makes deployment and notification safety visible without secrets. |
| P1 | Cursor pagination and evidence expansions | EXTEND | Needed for scale and incident investigation. |
| P2 | SSE event stream | FUTURE/EXTEND | Useful after polling and reconnect semantics are stable. |
| P2 | TUI adapter | NEW | Requires stable shared façade contracts. |
| P3 | Authenticated action API | FUTURE | Must be separately designed and approved. |
| P3 | AI Agent API | COMPLETE / PHASE 4B | Private authenticated read-only `/api/advisor/evaluate` landed at `af1a10b`. It has bounded transport validation, safe 400/401/422/500 errors, direct Phase 4A delegation, and no live collection, LLM, provider, action, or public-exposure path. |

## Cross-interface contract

The Web UI and TUI should consume the same Python capability methods wherever both run inside the AIPM process. A shared contract package should define:

- Domain models and enums.
- Safe status/error structures.
- Query objects with bounds.
- Capability protocol interfaces.
- Redaction and serialization rules.
- Freshness and pagination semantics.

The TUI may use direct façades for local efficiency. It should not make HTTP calls to localhost merely to reuse the web API, and it must not bypass façades to query repositories directly.

## Testing matrix for API additions

| Addition | Contract tests | Safety tests | Integration tests |
|---|---|---|---|
| Server | Shape, bounds, freshness | No arbitrary path/private data | Fake system provider and unavailable host |
| Docker detail | Shape, grouping, filters | No lifecycle calls/raw payloads | Fake Docker provider |
| Project detail | Shape, health/Git posture | No fetch/update | Fake Git/Compose provider |
| Systemd | Unit list/detail | No mutation verbs or env secrets | Fake manager adapter |
| Logs | Cursor, limits, truncation | Redaction and allow-list | Temporary journal/file fixture |
| Settings posture | Safe scalar fields | No raw config/secret refs | Temporary config with channels/policies |
| History compare | Bounded ranges/series | Read-only DB fingerprints | Active-WAL fixture |
| Incident evidence | Stable IDs/timeline | No acknowledgement/action route | Seeded MC-3 database |

## References

[1]: ../src/aipm/dashboard/server.py "Current route registration"
[2]: ../src/aipm/capabilities/dashboard/api.py "Overview façade"
[3]: ../src/aipm/capabilities/dashboard/incidents_api.py "Event and incident façade"
[4]: ../src/aipm/capabilities/dashboard/notifications_api.py "Notification safety façade"
[5]: ../src/aipm/capabilities/dashboard/history_api.py "History façade"
[6]: ../src/aipm/capabilities/dashboard/service_health_api.py "Service health façade"
[7]: ../src/aipm/repositories/readonly.py "Read-only filesystem boundary"
[8]: ../docs/MISSION_CONTROL.md "Existing MC-1 through MC-3 API contracts"
[9]: ../docs/MC-4_ARCHITECTURE.md "Existing MC-4 API contracts"
[10]: ../docs/MC-4.5_PRODUCTION_RUNBOOK.md "Notification hardening and production boundaries"

## Classification summary

- **EXISTS:** current overview, history, event, incident, notification, metrics, channel, policy, service-health, and static UI APIs.
- **EXTEND:** stable filters, cursor pagination, server/project/Docker detail, history comparisons, incident evidence, notification summary, and effective settings posture.
- **NEW:** shared TUI adapter and any later additive detail projections not already delivered.
**FUTURE:** public advisor exposure, Phase 4C+ API/UI integration, action routes, public authentication/ingress changes, SSE/WebSockets, notification activation, remediation, and LLM/provider integration. The private Phase 4B route is landed but remains non-public, read-only, and non-runtime.
