# MC-6.8 Design: Bounded, Redacted Logs Intelligence

**Design review status:** Complete and approved (historical design checkpoint)
**Design baseline:** `2384c83ab4d6ae74005ecf98416cff946d04cf87`
**Implementation status:** Complete and pushed at `d1f692948a014197eda60616fd602e8061959316`
**Original design scope:** Repository-grounded design for a read-only Logs façade, API, and page. The original design did not authorize source, test, configuration, runtime, deployment, VPS, database, Docker, Cloudflare, credential, or MC-6.9 changes.
**Current status note:** The approved implementation is now recorded in the repository. MC-6.9 remains unstarted and requires a separate design/inspection approval.

## 1. Exact roadmap objective

The authoritative Mission Control implementation plan defines **MC-6.8 as a new bounded, redacted, read-only Logs façade/API and page**. Its implementation sequence is to define a symbolic source registry; define maximum line, byte, time, and cursor limits; implement journald and approved-file adapters behind a protocol; redact before mapper serialization; add safe routes and UI; and link logs to existing incident/event identifiers where possible.[^plan]

The repository API gap analysis confirms that no general bounded log API exists today. It proposes an allow-listed symbolic `source` identifier, a `ReadOnlyLogService`, `DashboardLogsApi`, typed `LogEntry`/`LogPage`/`LogCursor` contracts, redaction before serialization, truncation markers, and a route of the form:

```text
GET /api/logs?source=aipm-dashboard&since=...&limit=200&cursor=...
```

The source identifier is a backend-owned symbolic ID. It is not an arbitrary filesystem path, unit name, shell expression, or provider argument supplied verbatim by the browser.[^gap]

## 2. Reconciliation with completed MC-5 through MC-6.7.1

MC-5 through MC-6.7.1 already provide the read-only foundation that MC-6.8 must reuse. The current system includes FastAPI routes, typed dataclass models, capability façades, bounded query validation, safe mappers, shared Observation/freshness semantics, the vanilla shell, centralized scheduler, Docker intelligence, Project/Application intelligence, Server/Host intelligence, and a backend-owned Systemd observation registry.

The current dashboard exposes Docker inventory and container detail, but the core Docker service also contains broader operations and a raw log reader. MC-6.8 must not route browser requests through that broad service because it mixes read and lifecycle methods and returns raw provider text. Docker log access, if later approved as a source, must be placed behind the new symbolic log registry and a read-only adapter that returns bounded, redacted entries only.

The existing AIPM logger uses a rotating file handler and the configuration model defines a default AIPM log file under the local AIPM state directory. This is evidence for a possible approved AIPM file source, not permission to expose arbitrary paths. The repository contains no general journald/file log façade today.

The completed MC-6.7.1 Systemd intelligence observes only genuine allow-listed native Systemd units. It does not make Docker containers into Systemd services. Cloudflared remains Docker-owned unless a real Systemd unit independently exists, and MC-6.8 must preserve that ownership correction.

## 3. Capability classification

| Capability | Classification | Design treatment |
|---|---|---|
| Existing Observation and freshness contract | EXISTS | Reuse `Observation`, `ObservationState`, freshness, availability, transport, and semantic-error semantics. |
| Query bounds | EXISTS / EXTEND | Reuse `query_bounds.py`, including existing log line and byte validators; add only time/cursor validation if a genuine gap remains. |
| Secret/output safety scanner | EXISTS | Reuse and extend with log-specific redaction fixtures; redaction must happen before mapping and serialization. |
| FastAPI dashboard adapter | EXISTS / EXTEND | Add one additive GET route family without changing existing routes. |
| Dashboard capability façade pattern | EXISTS | Add `DashboardLogsApi` using the established façade boundary. |
| Vanilla shell/router | EXISTS / EXTEND | Replace only the Logs placeholder and preserve existing navigation and static-module conventions. |
| Centralized scheduler/state modules | EXISTS / EXTEND | Register one bounded logs resource; do not add a second timer or polling framework. |
| Docker intelligence | EXISTS | Reuse only bounded normalized Docker identity/status where a Docker log source is explicitly approved; do not call lifecycle methods. |
| Systemd intelligence | EXISTS | Reuse symbolic native-unit ownership only for explicitly approved journald unit sources; do not infer units from containers. |
| Project/Application intelligence | EXISTS | Reuse opaque project IDs and safe associations for optional project filtering/cross-links; do not rediscover projects in Logs. |
| Server/Host intelligence | EXISTS | Reuse only safe host/source metadata where needed; do not expose arbitrary filesystem inventories. |
| Telemetry/history seams | EXISTS / EXTEND | Reuse existing events/incidents/history identifiers for cross-links; do not persist a second log store. |
| Symbolic log source registry | NEW | Backend-owned source IDs, source kind, owner, safe label, and fixed adapter configuration. |
| Read-only journald/file adapters | NEW | Protocol-backed bounded adapters with fixed arguments/paths and failure isolation. |
| Redaction pipeline | NEW / EXTEND | Add deterministic secret, credential, authorization, destination, environment, command-line, and path redaction before serialization. |
| Logs domain models and mapper | NEW | Add bounded entries, pages, cursors, truncation metadata, source errors, and safe redaction metadata. |
| Log history database | UNAVAILABLE / OUT OF SCOPE | Current logs are queried from approved live sources; no new SQLite schema or writer is justified. |
| Log streaming/SSE/WebSocket | FUTURE | First implementation is bounded request/response only. |
| Log export/download | FUTURE / FORBIDDEN | No raw-log download or unbounded browser export. |
| Log mutation, rotation, vacuum, clear, or retention control | FUTURE / FORBIDDEN | No action API or filesystem mutation. |

## 4. Ownership boundaries

MC-6.8 must preserve the following ownership model:

| Domain | Authoritative owner | Logs relationship |
|---|---|---|
| Logical applications | Project/Application intelligence | Optional safe project filter and cross-link only. |
| Containers and stacks | Docker intelligence | Optional approved Docker source; normalized container identity only. |
| Native services | Systemd intelligence | Optional approved journald unit source; only real allow-listed units. |
| Host capacity and identity | Server/Host intelligence | Safe source context only; no arbitrary host path access. |
| Events/incidents | MC-3 event/incident services | Existing identifiers and evidence links only; no duplicate correlation. |
| Historical telemetry | Existing telemetry/history repositories | Existing history remains authoritative; no raw log persistence. |
| Cloudflared | Docker intelligence under the verified ownership correction | Do not create a Systemd log source merely because Cloudflared is operationally important. |

A log source must be represented only when its owner and source are explicitly registered by the backend. The browser may request a symbolic source ID from the registry but may never supply a path, unit string, container name, command, or provider-specific argument as a source definition.

## 5. Proposed architecture and data flow

```text
Browser
  │
  └── GET /api/logs?source=<opaque-id>&since=<bounded>&until=<bounded>&limit=<bounded>&cursor=<bounded>
        │
FastAPI dashboard adapter
        │
DashboardLogsApi
        │
ReadOnlyLogService
        │
Backend-owned LogSourceRegistry
        │
LogSourceProvider protocol
        ├── bounded journald adapter for approved native units
        ├── bounded file adapter for approved AIPM log files
        └── bounded Docker adapter only if explicitly approved
        │
Normalized LogPage
        │
Redaction pipeline
        │
Safe mapper and Observation envelope
        │
Vanilla Logs page / incident-event cross-links
```

The façade owns source lookup, query-bound enforcement, failure isolation, and semantic response construction. Adapters own only their fixed local read mechanism. The mapper receives already normalized and redacted data; it must not be responsible for discovering paths or interpreting arbitrary provider output.

The first implementation should use bounded polling or explicit user refresh, not an unbounded tail. A page navigation or filter change may request a new bounded page, but it must not open a persistent stream. The existing scheduler may refresh the current source at a conservative cadence only if the UX review demonstrates that polling is useful; one resource registration is permitted and duplicate timers are forbidden.

## 6. Proposed additive API contracts

### `GET /api/logs`

The initial route should accept only bounded query parameters:

```text
GET /api/logs
  ?source=<opaque source ID>
  &since=<bounded timestamp or relative range>
  &until=<bounded timestamp, optional>
  &severity=<allow-listed enum, optional>
  &unit=<opaque allow-listed unit ID, optional>
  &project=<opaque project ID, optional>
  &limit=<bounded integer>
  &cursor=<opaque bounded cursor, optional>
```

The browser must not submit an arbitrary path, unit name, container name, executable, regular expression, journal field, or shell fragment. Server-side validation is mandatory even when client-side controls appear bounded.

Illustrative response shape:

```json
{
  "observation": {
    "state": "fresh",
    "available": true,
    "transport_ok": true,
    "observed_at": "2026-08-18T12:00:00Z",
    "age_seconds": 2,
    "max_age_seconds": 90,
    "error": null
  },
  "source": {
    "id": "aipm-dashboard",
    "label": "AIPM Dashboard",
    "kind": "journald",
    "owner": "systemd"
  },
  "entries": [
    {
      "timestamp": "2026-08-18T11:59:58Z",
      "severity": "warning",
      "message": "upstream request redacted [REDACTED_URL]",
      "redacted": true,
      "evidence": ["external_url"]
    }
  ],
  "next_cursor": null,
  "truncated": false,
  "returned_lines": 1,
  "returned_bytes": 74,
  "errors": []
}
```

The exact JSON names must follow repository conventions. `message` is normalized, bounded, and redacted. Raw stdout/stderr, raw journal fields, environment values, command lines, unit-file contents, private paths, secret references, and arbitrary provider metadata are excluded.

### Source registry projection

The route may expose a separate safe source list or embed the selected source projection in the page response. It must include only backend-owned symbolic IDs, safe labels, source kind, owner, and availability posture. It must not expose configured filesystem paths, raw unit names where an opaque label is sufficient, journal match expressions, credentials, or adapter arguments.

### Future detail/cross-link routes

Incident/event cross-links should use existing safe identifiers rather than creating a second log identity model. Candidate future additive routes include:

```text
GET /api/incidents/{incident_id}/logs
GET /api/events/{event_id}/logs
```

These are design candidates only and should not be added in the first MC-6.8 slice unless the existing incident/event façades can provide the links without new storage or correlation logic.

## 7. Domain models and ownership boundaries

The proposed typed contracts are:

| Model | Required fields and constraints |
|---|---|
| `LogSourceId` | Opaque backend-owned stable identifier; never a path or arbitrary unit string. |
| `LogSourceKind` | Safe enum such as `journald`, `file`, or explicitly approved `docker`. |
| `LogSourceRegistryEntry` | ID, display label, kind, owner domain, fixed internal source reference, and safe availability metadata. The fixed reference is never serialized. |
| `LogQuery` | Validated source, bounded time range, severity enum, optional opaque unit/project filters, limit, and cursor. |
| `LogEntry` | Bounded timestamp, severity, normalized message, redaction flag, and safe redaction categories. |
| `LogCursor` | Opaque, signed or integrity-checked, bounded cursor with no raw path or query injection. |
| `LogPage` | Entries, next cursor, truncation metadata, returned line/byte counts, and source-specific errors. |
| `LogObservationError` | Safe category/message; never raw exception text or provider output. |

The `ReadOnlyLogService` owns orchestration and redaction ordering. Adapters must not return unbounded strings. The `DashboardLogsApi` owns query bounds and safe envelopes. The mapper owns output allow-listing, not source discovery. No log model owns writes, retention, rotation, deletion, or notification delivery.

## 8. Freshness, unavailable, stale, unknown, and error semantics

A successful bounded read from an available approved source is `fresh` when it is within the configured observation age. A successful empty result remains available and fresh with an explicit empty state; it must not be confused with an unavailable source.

A source that cannot be read because the journal/file is unavailable, permissions are insufficient, the fixed adapter times out, the source is malformed, or the source is not supported returns an explicit `unavailable` or semantic `error` state with a safe source-specific message. One source failure must not invalidate other registered sources.

`stale` is reserved for a previously obtained page that is displayed beyond its freshness threshold. The first implementation should avoid an application cache; if a stale result is ever retained by the frontend, it must be marked stale rather than presented as current.

`never_sampled` means no successful observation has existed for the selected source. `unknown` means the source or state cannot be semantically determined without guessing. A log line containing an application word such as “healthy” does not change the source observation state or infer service health.

Malformed or tampered cursors, excessive time windows, excessive line/byte limits, unknown source IDs, arbitrary paths, unsupported unit filters, and invalid severity values fail closed with bounded safe errors. No request may silently widen its query.

## 9. Security and read-only boundaries

Logs are high-risk data. Redaction is a mandatory security boundary, not a display enhancement. The pipeline must run before mapper serialization and before the response reaches the browser. It must cover, at minimum, credential-like key/value pairs, bearer and authorization material, tokens, passwords, API keys, webhook/destination URLs, private or local paths when not required, environment assignments, command-line fragments, and common secret formats. Redaction must preserve bounded message length and indicate that content was removed.

The source registry must be backend-owned. A file adapter may open only fixed approved files or paths derived from a fixed internal registry entry. It must never accept a browser path, follow arbitrary path traversal, dereference uncontrolled symlinks, walk a directory, or read a file selected by a raw query parameter.

A journald adapter may query only fixed approved unit/source matches. It must construct fixed arguments without shell interpretation, use bounded timeouts, cap bytes and lines, and avoid returning arbitrary journal fields. A Docker adapter, if approved, must use only an existing observation seam and must never expose Docker SDK raw payloads or call `logs` through a service that also makes lifecycle methods reachable.

The following are explicit non-goals and forbidden operations:

- No `POST`, `PUT`, `PATCH`, or `DELETE` log routes.
- No start, stop, restart, reload, enable, disable, reset-failed, Docker lifecycle, Compose mutation, Git mutation, or notification action.
- No arbitrary shell, `journalctl` command string, process execution, `docker exec`, or command-line inspection.
- No arbitrary filesystem path, recursive directory traversal, unrestricted file tailing, raw log download, or unbounded export.
- No environment-variable, credential, token, authorization, destination, or raw provider-payload exposure.
- No new worker, stream, WebSocket, SSE, database, schema, log archive, or duplicate telemetry collector unless a later design proves it necessary and receives separate approval.
- No live SQLite writes, schema initialization, migration, checkpoint, or sidecar mutation from dashboard log reads.
- No Cloudflare API, public ingress, provider credential, or network operation.

## 10. Frontend information architecture and UX

The existing `#/logs` navigation slot should become a functional **Logs** page without replacing the vanilla shell. The page should prioritize diagnosis while making the read-only boundary visible:

1. A source selector populated only from the backend-owned symbolic registry.
2. Bounded filters for time range, severity, native unit where explicitly approved, logical project, and page size.
3. A freshness/availability header showing source, observation age, returned line/byte counts, truncation, and redaction status.
4. A bounded log table or card list with timestamp, severity, normalized message, and a redaction indicator.
5. Explicit empty, unavailable, stale, never-sampled, unknown, malformed-cursor, and error states.
6. An operator-visible statement that Logs is observation-only and provides no download, clear, rotate, retry-action, restart, or remediation controls.
7. Optional links to existing event/incident detail only when the link is based on an existing safe identifier; no new correlation is inferred in the browser.

The UI must escape text before rendering. It must not use `innerHTML` with raw log messages, interpret ANSI/control sequences as markup, render URLs as clickable destinations without review, or expose unredacted raw content through tooltips, DOM attributes, browser logs, or download links. Local Projects, Docker, and Systemd ownership labels must remain distinct: a log source labelled Docker must not be presented as a native Systemd unit.

The page may use the centralized scheduler for a conservative current-page refresh, but it must not create a second polling loop. Pagination and cursor navigation should be explicit user actions or bounded resource refreshes, not an unbounded tail.

## 11. Reusable repository components

| Existing component | MC-6.8 reuse |
|---|---|
| `src/aipm/models/mission_control.py` | Observation, freshness, availability, transport, and semantic-error states. |
| `src/aipm/capabilities/dashboard/query_bounds.py` | Existing `MAX_LOG_LINES`, `MAX_LOG_BYTES`, and log validators; extend only for bounded time/cursor validation if required. |
| `src/aipm/capabilities/dashboard/safety.py` | Secret/output scanning and response-safety fixtures. |
| `src/aipm/dashboard/server.py` | FastAPI adapter, additive GET route, static mount, and application composition. |
| `src/aipm/dashboard/static/mission-control-shell.js` | Existing Logs navigation and hash routing. |
| `src/aipm/dashboard/static/mission-control-state.js` | UI normalization for fresh/stale/unavailable/never-sampled/unknown/error. |
| `src/aipm/dashboard/static/mission-control-scheduler.js` | One bounded Logs resource, visibility handling, deduplication, backoff, and cleanup. |
| Existing Docker intelligence | Safe normalized container identity only if a Docker source is explicitly registered; never lifecycle methods or raw logs. |
| Existing Project/Application intelligence | Opaque project IDs and safe association filters only; no duplicate discovery. |
| Existing Systemd intelligence | Genuine allow-listed native unit ownership and safe labels only; no inferred Cloudflared Systemd source. |
| Existing Server/Host intelligence | Safe host/source context only; no arbitrary path enumeration. |
| Existing history/events/incidents services | Existing identifiers, freshness, and cross-link projections; no second log store. |
| Existing AIPM logger/config | Candidate fixed AIPM log source; configuration path must be internal and never serialized. |

## 12. Proposed implementation sequence

The later approved implementation should proceed as small slices:

1. Add typed source IDs, source registry, query/page/cursor models, safe error categories, and redaction categories.
2. Confirm and reuse existing log bounds; add bounded timestamp/cursor helpers only where necessary.
3. Implement fake-provider tests before local adapters, covering redaction, limits, cursors, truncation, malformed input, and failure isolation.
4. Implement a read-only journald adapter for a small explicitly approved set of genuine Systemd units and a fixed AIPM file adapter only if its path is safely defined. Do not add arbitrary Docker logs in the first slice unless a separate source review approves it.
5. Implement `ReadOnlyLogService`, redaction-before-mapping, and `DashboardLogsApi`.
6. Add the additive `GET /api/logs` route with server-side bounds and safe source projection.
7. Replace only the Logs placeholder with the vanilla bounded page, static `/static` module, shared state, and at most one scheduler resource.
8. Add optional event/incident cross-links only from existing safe identifiers after the base route is stable.
9. Run focused tests, MC-5 through MC-6.7.1 regressions, full suite, compilation, JavaScript syntax, diff checks, mutation/subprocess scans, output-safety scans, frontend action scans, and temporary SQLite fingerprint tests.
10. Stop for review before any commit, push, VPS staging, deployment, public ingress, or MC-6.9 work.

## 13. Testing strategy

| Layer | Required validation |
|---|---|
| Registry/model | Only symbolic sources resolve; unknown IDs fail closed; all fields and enums are bounded and stable. |
| Query bounds | Excessive lines, bytes, time windows, page sizes, cursors, invalid severities, arbitrary paths, and unsupported filters fail closed. |
| Adapter | Fixed arguments/paths, `shell=False` where subprocesses are used, short timeout, bounded output, no lifecycle verbs, no arbitrary source input, and failure isolation. |
| Redaction | Secrets, tokens, credentials, authorization, destinations, environment values, command lines, private paths, and control characters do not reach serialized responses or DOM. |
| Service/façade | Redaction precedes mapping; source failures remain source-specific; no write/retry/action methods are reachable. |
| API | GET-only route, bounded JSON envelope, safe errors, truncation markers, cursor behavior, empty/unavailable/stale/unknown states, and action-route absence. |
| Frontend | `/static` module routing, source selector, filters, escaping, redaction indicators, bounded rendering, scheduler deduplication, responsive layout, and no download/action controls. |
| Regression | MC-5 through MC-6.7.1 tests, existing Docker/Project/Server/Systemd behavior, history/events/incidents/notifications, and read-only repository suites. |
| Integration | Fake journald/file providers, temporary approved files, seeded data, no live journal, no live database, no Docker daemon, no credentials, and no external network. |
| Deployment verification | Local loopback only, temporary configuration, port/process cleanup, no live log reads, no systemd unit changes, and rollback evidence if a temporary service is later approved. |

## 14. Database, deployment, and rollback considerations

MC-6.8 should not add a database or schema. Logs are queried from bounded approved local sources and are not copied into telemetry SQLite. Existing history, events, and incidents remain authoritative for persisted evidence. Dashboard construction and GET requests must continue to use the validated read-only repository boundary and must not create files, migrate schemas, checkpoint WAL, or write sidecars.

Local validation must use temporary configuration and fake/seeded sources. No live journal, live file, live SQLite, Docker daemon, provider credential, VPS runtime, or external network is required for the implementation gate.

If a future deployment is separately approved, it must remain loopback-only and use the existing read-only dashboard service topology. Rollback consists of restoring the previous application commit/static asset, stopping only the introduced dashboard process if applicable, removing only a newly introduced persistent unit/configuration fragment, confirming port closure, and proving telemetry, MC-3, notifications, Docker, Cloudflared ownership, credentials, and SQLite fingerprints are unchanged. Log rollback must never delete, truncate, rotate, or checkpoint source logs as part of ordinary UI failure handling.

## 15. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Secrets appear in operational logs | Redact before mapping, use deterministic patterns, mark redaction, and fail closed on unsafe fields. |
| A file path exposes private host layout | Backend-owned fixed paths only; never serialize paths or accept browser paths. |
| Journald query becomes an arbitrary command channel | Symbolic source registry, fixed arguments, `shell=False`, timeout, bounded output, and no raw command input. |
| Logs overwhelm memory/browser | Enforce line/byte/time limits server-side, truncate explicitly, paginate with opaque cursors, and avoid streams. |
| Docker/Systemd ownership is conflated | Preserve domain ownership; only genuine allow-listed Systemd units become journald sources; Cloudflared remains Docker-owned. |
| Redaction creates misleading evidence | Keep redaction metadata and safe categories; never claim missing content is absent. |
| One unavailable source breaks the Logs page | Per-source failure isolation and explicit unavailable/error envelopes. |
| Cursor tampering or replay | Use bounded opaque integrity-checked cursors with source/filter binding and short validity if needed. |
| UI XSS/control-character injection | Escape text, avoid raw HTML insertion, sanitize control characters, and test malicious log fixtures. |
| New polling timer accumulates | Reuse the centralized scheduler with one resource and lifecycle tests. |

## 16. Explicit non-goals and unavailable areas

MC-6.8 does not add lifecycle controls, arbitrary shell or process execution, mutation APIs, credentials, secret exposure, unrestricted filesystem access, arbitrary Docker logs, raw journal export, downloads, log deletion/rotation, new workers, duplicate telemetry collectors, a new database/schema, a log archive, a second scheduler, public ingress, authentication, Cloudflare changes, notification activation, AI actions, or MC-6.9 incident/history expansion.

Network access, target-VPS journald behavior, exact production log volume, source permissions, and public-ingress relationships are unavailable in this design-only sandbox and must not be assumed. They require a separate read-only preflight after implementation approval, without weakening the fail-closed design.

## 17. Exact proposed file changes

The expected first implementation files are:

```text
docs/MC-6.8_DESIGN.md                         # this design document; design-only now
src/aipm/models/logs.py                       # new typed models and source registry
src/aipm/providers/logs.py                    # new bounded adapter protocol/local adapters
src/aipm/services/logs/observation.py         # new read-only orchestration/redaction service
src/aipm/mappers/logs.py                      # new safe response mapper
src/aipm/capabilities/dashboard/logs_api.py  # new bounded GET-only façade
src/aipm/dashboard/server.py                  # additive GET /api/logs route only
src/aipm/dashboard/static/index.html          # replace only the Logs placeholder
src/aipm/dashboard/static/mission-control-logs.js # new vanilla Logs controller
tests/test_mc68_logs.py                       # focused models/provider/service/API/frontend tests
```

Existing files must remain unchanged unless a focused compatibility test proves a minimal additive seam is required. No Systemd, Docker, Cloudflare, notification, database, deployment-template, or production-runtime file is expected to change for the first Logs slice.

## 18. Stop condition before MC-6.9

MC-6.8 design/implementation is complete only when the bounded Logs contract, redaction behavior, source ownership, API, UI, and safety gates have been reviewed and validated. After the first implementation and local validation, stop before commit or push for review. Target-VPS staging, deployment, public access, log-source permission changes, and any persistence extension require separate approvals.

**MC-6.9 must not start** until MC-6.8 has a reviewed implementation, focused and full regression results, output-safety evidence, frontend acceptance, and an explicit checkpoint approval. MC-6.9 may extend incident/history evidence only through existing repositories and contracts; it must not be used to bypass unresolved Logs safety issues.

## 19. Design-only validation markers

```text
ONE_FILE_SCOPE=PASS
DESIGN_CONTENT=PASS
DIFF_CHECK=PASS
SOURCE_CHANGES=0
TEST_CHANGES=0
RUNTIME_CHANGES=0
DEPLOYMENT_CHANGES=0
MC6.9_STARTED=NO
GATE_2_1_HARNESS=UNCHANGED
```

## References

[^plan]: [MC-6 implementation plan](MC-6_IMPLEMENTATION_PLAN.md), MC-6.8 — Logs.
[^gap]: [MC-6 API gap analysis](MC-6_API_GAP_ANALYSIS.md), Logs section and contract invariants.
[^architecture]: [MC-6 architecture](MC-6_ARCHITECTURE.md), ownership, read-only security, and shared façade boundaries.
[^server]: [Dashboard FastAPI adapter](../src/aipm/dashboard/server.py), existing additive GET route and static-shell conventions.
