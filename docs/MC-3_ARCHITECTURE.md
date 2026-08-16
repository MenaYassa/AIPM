# Mission Control MC-3 Architecture Assessment

## Approval gate

MC-2 is closed in the existing `MenaYassa/AIPM` repository. The verified commit is:

```text
151a855 feat: add Mission Control historical telemetry
```

The commit is pushed to `origin/main`, the local working tree is clean, and `pytest -q` reports 51 passing tests with the known Starlette/httpx warning. No MC-3 implementation code has been added.

This assessment proposes MC-3, **Event Engine & Incident Room**, on top of the committed MC-2 architecture. Coding should begin only after this document is reviewed and approved.

> MC-1.5 observes current state. MC-2 remembers normalized history. MC-3 derives deterministic change and groups it into incidents.

## 1. Current architecture after MC-2

The current AIPM architecture is:

```text
Application
  ├── ConfigManager / shared logger
  ├── SystemService
  └── DockerService

DashboardApi
  ├── DashboardTelemetryService
  │     ├── HostTelemetryService
  │     ├── DockerTelemetryService
  │     ├── ProjectTelemetryService
  │     └── TunnelTelemetryService
  └── DashboardHistoryApi
        └── HistoricalQueryService
              └── SQLiteHistoryRepository

DashboardSnapshot
  ↓
TelemetryHistoryMapper
  ↓
TelemetrySampler
  ↓
SQLite telemetry database
```

The MC-2 telemetry database currently contains normalized `sample_runs`, `host_samples`, `container_samples`, `project_samples`, and `tunnel_samples` tables. The sampler is intentionally limited to collection, mapping, persistence, and timestamp-based telemetry retention. It contains no event interpretation.

The existing dashboard API preserves `/healthz`, `/api/overview`, `/`, and the four history endpoints. The new MC-3 layer must not break those routes.

## 2. Existing Health Engine integration points

The existing Health Engine is deterministic and already composes three analyzers:

```text
HealthEngine
  ├── GitAnalyzer
  ├── ComposeAnalyzer
  └── DockerAnalyzer
        ↓
Finding[]
        ↓
ReportBuilder
        ↓
HealthReport
```

`HealthEngine.analyze(project)` catches analyzer failures and converts them into a structured warning `Finding` with code `ANALYZER_FAILED`. `ReportBuilder` calculates severity counts, a score, and the existing `HealthState` using the current fixed penalties and thresholds. MC-3 should reuse this engine rather than create a parallel health evaluator.

The clean integration point is an **event-processing service** that receives the current project from a reconstructed historical frame or current `DashboardSnapshot`, calls `HealthEngine` for deterministic evidence, and compares the current health observation to the previous persisted health observation. `TelemetrySampler` remains unchanged.

Health analysis is read-only in the current architecture. `GitAnalyzer` consumes the already available `Project.git` snapshot, while `ComposeAnalyzer` and `DockerAnalyzer` inspect current Compose/Docker state. MC-3 must not call Git fetch/pull or any Docker/Compose mutation.

## 3. Existing Finding model capabilities

The existing `Finding` model provides:

| Field | MC-3 use |
|---|---|
| `code` | Stable deterministic evidence identifier, such as `CONTAINER_UNHEALTHY` or `GIT_DIRTY`. |
| `component` | Existing analyzer/component identity. |
| `severity` | Reuse existing `Severity` enum: `INFO`, `WARNING`, `HIGH`, `CRITICAL`. |
| `title` | Human-readable event/incident evidence title. |
| `description` | Evidence details. |
| `recommendation` | Existing health output only; MC-3 must not turn it into an automatic action. |
| `resource` | Resource reference, usually a container name or component identifier. |

MC-3 should preserve the full Finding as structured evidence attached to event derivation, but it should not duplicate `Severity`, `HealthState`, or resource semantics. Event severity should reuse the existing `Severity` enum. Incident status is a separate workflow enum and must not be conflated with health state.

## 4. Exact current telemetry schema

MC-2 uses the following SQLite schema.

### `sample_runs`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
sampled_at INTEGER NOT NULL
host_available INTEGER NOT NULL
docker_available INTEGER NOT NULL
projects_available INTEGER NOT NULL
tunnel_state TEXT NOT NULL
duration_ms INTEGER
```

### `host_samples`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
run_id INTEGER NOT NULL REFERENCES sample_runs(id) ON DELETE CASCADE
sampled_at INTEGER NOT NULL
hostname TEXT
cpu_percent REAL
load_one REAL
load_five REAL
load_fifteen REAL
memory_total_gb REAL
memory_used_gb REAL
memory_available_gb REAL
memory_percent REAL
swap_total_gb REAL
swap_used_gb REAL
swap_percent REAL
disk_total_gb REAL
disk_used_gb REAL
disk_free_gb REAL
disk_percent REAL
network_interfaces INTEGER
network_established INTEGER
available INTEGER NOT NULL
```

### `container_samples`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
run_id INTEGER NOT NULL REFERENCES sample_runs(id) ON DELETE CASCADE
sampled_at INTEGER NOT NULL
container_id TEXT NOT NULL
container_name TEXT NOT NULL
image TEXT
state TEXT
health TEXT
stack TEXT
restart_count INTEGER
cpu_percent REAL
memory_used_mb REAL
memory_limit_mb REAL
memory_percent REAL
stats_available INTEGER NOT NULL
```

### `project_samples`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
run_id INTEGER NOT NULL REFERENCES sample_runs(id) ON DELETE CASCADE
sampled_at INTEGER NOT NULL
name TEXT NOT NULL
path TEXT
branch TEXT
has_git INTEGER NOT NULL
has_compose INTEGER NOT NULL
dirty INTEGER
ahead INTEGER
behind INTEGER
```

### `tunnel_samples`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
run_id INTEGER NOT NULL REFERENCES sample_runs(id) ON DELETE CASCADE
sampled_at INTEGER NOT NULL
state TEXT NOT NULL
source TEXT NOT NULL
systemd TEXT
local_containers TEXT
```

Existing indexes cover sample timestamps, container ID/name plus timestamp, project name plus timestamp, and tunnel timestamp. MC-3 should add event/incident indexes without changing the meaning of the telemetry tables.

## 5. How previous/current samples can be compared

MC-3 should not compare isolated current values. It should compare adjacent observations for the same resource and source run.

A `HistoricalFrameService` should reconstruct a normalized comparison frame for a source `sample_run`:

```text
source run N
  ├── current container rows
  ├── previous container row per stable container_id
  ├── current project rows
  ├── previous project row per stable project path/name
  ├── current tunnel row
  └── previous tunnel row
```

The comparison rules are:

| Resource | Stable comparison key | Previous/current source |
|---|---|---|
| Container | `container_id` | Adjacent `container_samples` rows ordered by `sampled_at`, with `run_id` as the source identity. |
| Project | `path` when present, otherwise `name` | Adjacent `project_samples` rows ordered by `sampled_at`. |
| Tunnel | Singleton local tunnel subject | Adjacent `tunnel_samples` rows ordered by `sampled_at`. |
| Health observation | `project_path` plus finding fingerprint | MC-3 health observation repository rows, generated by the existing Health Engine. |

If a container ID disappears or a new ID appears, MC-3 must not infer a stop/start/restart lifecycle event from name matching. Container recreation identity belongs to a later lifecycle milestone. If a component is unavailable or a row is missing, the event engine should produce no transition rather than inventing a stopped or recovered state.

The event processor should process one source `sample_run` at a time. It should obtain the previous facts through a repository/query boundary and commit event/state changes atomically for that run.

## 6. Event domain model

The internal representation should be strongly typed and should not use arbitrary dictionaries.

Proposed models:

```text
Event
  id: int | None
  event_key: str
  occurred_at: datetime
  event_type: EventType
  severity: Severity
  source: EventSource
  resource: ResourceRef
  title: str
  description: str
  previous_value: str | None
  current_value: str | None
  source_run_id: int
  previous_run_id: int | None
  correlation_key: str
  evidence: tuple[FindingEvidence, ...]
```

Supporting typed models:

```text
ResourceRef
  resource_type: ResourceType
  identifier: str
  name: str | None
  project_path: str | None

FindingEvidence
  code: str
  severity: Severity
  component: str
  title: str
  description: str
  resource: str | None
```

Existing `Container`, `Project`, `Finding`, `Severity`, and `HealthState` types should be referenced or composed rather than recreated. Event details should remain typed fields or typed evidence objects, not an unbounded JSON dictionary.

## 7. Initial event types and deterministic rules

MC-3 should begin with the smallest set reliably derivable from current MC-2 data and the existing Health Engine.

| Event type | Deterministic rule | Initial severity basis |
|---|---|---|
| `ContainerStarted` | Same `container_id`; previous state is not `running` and current state is `running`, excluding `restarting → running`, which is recovery. | `INFO` unless current health is unhealthy. |
| `ContainerRestarting` | Same `container_id`; previous state is not `restarting` and current state is `restarting`. | `HIGH`. |
| `ContainerRestarted` | Same `container_id`; current `restart_count` is greater than previous `restart_count`. One event per observed counter transition. | `WARNING` or `HIGH` depending on existing Finding evidence. |
| `ContainerStopped` | Same `container_id`; previous state is `running` or `restarting` and current state is `exited` or `dead`. | `HIGH`. |
| `ContainerRecovered` | Same `container_id`; previous state is `restarting` and current state is `running`, or health changes from `unhealthy` to `healthy`. | `INFO`. |
| `ContainerHealthChanged` | Same `container_id`; both health values are present and differ. | `WARNING` for degraded/unhealthy; `INFO` for recovery. |
| `ProjectGitStateChanged` | Same project key; branch, dirty, ahead, or behind state changes. No remote fetch is performed. | `WARNING` when becoming dirty/behind/conflicted evidence exists; otherwise `INFO`. |
| `TunnelStateChanged` | Previous and current local tunnel state are both known and differ. | `HIGH` when entering `down`; `INFO` on recovery. |
| `HealthStateChanged` | Current `HealthReport.state` differs from the previous persisted health observation for the same project. Uses existing `HealthState`. | Map only for incident presentation; do not redefine HealthState. |
| `HealthFindingChanged` | The deterministic set of Finding fingerprints for a project/resource changes. | Reuse the Finding severity. |

The following are explicitly deferred because MC-2 cannot reliably derive them without new facts or thresholds:

| Deferred type | Reason |
|---|---|
| `ComposeStateChanged` | `project_samples` stores project capability/Git state but not Compose service state history. |
| `ResourceThresholdCrossed` | No approved threshold configuration exists, and MC-3 must not invent alert semantics. |
| Container lifecycle events for new IDs | Name matching cannot reliably distinguish recreation from rename/replacement. |
| Causal root-cause events | MC-3 must not claim causality without deterministic evidence. |

## 8. Event derivation service

The proposed `EventDerivationService` is a pure deterministic component:

```text
current comparison frame
      +
previous comparison frame
      +
current HealthReport/evidence
      ↓
EventDerivationService
      ↓
tuple[Event, ...]
```

It must not call psutil, Docker, Git, Compose, systemd, Cloudflare, or any mutating operation. It should not access SQLite directly. It should receive typed comparison data and typed Health Engine output.

Repeated state must not produce events. A `restarting` container observed for ten consecutive samples produces one `ContainerRestarting` event on entry, not ten events. A `restart_count` increase produces an event only when the observed counter transitions upward. Recovery is emitted only when the explicit recovery transition occurs.

## 9. Event processing execution choice

Two execution options were evaluated.

| Option | Advantages | Risks |
|---|---|---|
| Derive events immediately after every sampler cycle in the same process | Lowest latency and fewer processes. | Couples event interpretation to sampling, complicates retries, and risks violating the sampler’s stable collect/map/persist boundary. |
| Run a separate deterministic event processor over persisted sample runs | Keeps the sampler factual and stable, permits retries/reprocessing, and gives idempotency a clear source-run boundary. | Adds a second systemd-managed process and requires a high-watermark/claim table. |

**Recommendation: choose the separate event processor.** MC-2’s sampler remains unchanged. A dedicated `aipm events run` process polls persisted `sample_runs`, processes each unprocessed run once, and commits event/state changes transactionally. A one-shot `aipm events process` command supports tests and operator verification.

The event processor must not create duplicate events if it restarts or two invocations see the same run. SQLite’s single-writer behavior plus unique identities and an atomic processing marker provide the first implementation’s safety boundary.

## 10. Event persistence schema

Events should use the same configured telemetry SQLite database, but separate repository and domain boundaries.

### `events`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
event_key TEXT NOT NULL UNIQUE
occurred_at INTEGER NOT NULL
event_type TEXT NOT NULL
severity TEXT NOT NULL
source TEXT NOT NULL
resource_type TEXT NOT NULL
resource_id TEXT NOT NULL
resource_name TEXT
project_path TEXT
title TEXT NOT NULL
description TEXT NOT NULL
previous_value TEXT
current_value TEXT
source_run_id INTEGER NOT NULL REFERENCES sample_runs(id)
previous_run_id INTEGER
correlation_key TEXT NOT NULL
created_at INTEGER NOT NULL
```

### `event_evidence`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE
finding_code TEXT NOT NULL
component TEXT NOT NULL
severity TEXT NOT NULL
title TEXT NOT NULL
description TEXT NOT NULL
resource TEXT
```

### `event_processing_runs`

```text
source_run_id INTEGER PRIMARY KEY REFERENCES sample_runs(id) ON DELETE CASCADE
processed_at INTEGER NOT NULL
status TEXT NOT NULL
```

The primary key on `source_run_id` prevents the same source run from being committed twice. `events.event_key` is a second idempotency guard for deterministic reprocessing and protects against partial retry paths.

Proposed indexes:

```text
idx_events_occurred_at             (occurred_at)
idx_events_resource_time           (resource_type, resource_id, occurred_at)
idx_events_correlation_time        (correlation_key, occurred_at)
idx_events_source_run              (source_run_id)
idx_events_type_time               (event_type, occurred_at)
idx_event_evidence_event           (event_id)
```

The event repository must use parameterized SQL and a transaction that atomically records processing state, events, and evidence. There must be no event records inside MC-2 telemetry tables.

## 11. Health observation integration

MC-2 does not persist HealthReports or Findings. To support deterministic health transitions without duplicating the Health Engine, MC-3 should add a small normalized health evidence layer:

### `health_observations`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
source_run_id INTEGER NOT NULL REFERENCES sample_runs(id) ON DELETE CASCADE
sampled_at INTEGER NOT NULL
project_path TEXT NOT NULL
project_name TEXT NOT NULL
report_state TEXT NOT NULL
score INTEGER NOT NULL
```

### `health_findings`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
observation_id INTEGER NOT NULL REFERENCES health_observations(id) ON DELETE CASCADE
finding_fingerprint TEXT NOT NULL
code TEXT NOT NULL
component TEXT NOT NULL
severity TEXT NOT NULL
title TEXT NOT NULL
description TEXT NOT NULL
resource TEXT
```

The event processor runs the existing `HealthEngine` for the current project snapshot, stores normalized evidence, and compares the current observation to the previous observation for that project. The event engine reuses existing `HealthState`, `Severity`, and Finding fields; it does not create another health taxonomy.

A health finding fingerprint should be deterministic, for example:

```text
code + component + resource + severity + normalized title
```

Descriptions and recommendations should be evidence presented to the incident room, not identity inputs if they are likely to vary in wording.

## 12. Idempotency strategy

The event identity is deterministic:

```text
hash(
  source_run_id,
  previous_run_id,
  event_type,
  resource_type,
  resource_id,
  transition_signature
)
```

The human-readable form may be retained for diagnostics, while the database stores a stable fixed-length key. The same source run, previous state, transition type, and resource therefore yield the same `event_key` on retries.

Processing is idempotent at two levels:

1. `event_processing_runs.source_run_id` ensures one source run is committed once.
2. `events.event_key` ensures duplicate event insertion is ignored even if processing is retried after a partial read or deployment restart.

A successful event-processing transaction must commit the processing marker, derived events, event evidence, and health observations together. If it fails, the source run remains retryable.

## 13. Incident model

The initial Incident model should be strongly typed:

```text
Incident
  id: int | None
  incident_key: str
  title: str
  severity: Severity
  status: IncidentStatus
  started_at: datetime
  updated_at: datetime
  resolved_at: datetime | None
  resource: ResourceRef
  correlation_key: str
  summary: str
```

`IncidentStatus` should initially contain `OPEN`, `ACKNOWLEDGED`, and `RESOLVED`. Acknowledgement is metadata only; it must never execute remediation. There will be no restart, stop, start, update, rollback, shell, docker exec, backup, or restore action.

Related event IDs should be stored in a join table rather than an opaque list field.

## 14. Incident persistence schema

### `incidents`

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
incident_key TEXT NOT NULL UNIQUE
title TEXT NOT NULL
severity TEXT NOT NULL
status TEXT NOT NULL
started_at INTEGER NOT NULL
updated_at INTEGER NOT NULL
resolved_at INTEGER
resource_type TEXT NOT NULL
resource_id TEXT NOT NULL
resource_name TEXT
project_path TEXT
correlation_key TEXT NOT NULL
summary TEXT NOT NULL
```

### `incident_events`

```text
incident_id INTEGER NOT NULL REFERENCES incidents(id) ON DELETE CASCADE
event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE
attached_at INTEGER NOT NULL
PRIMARY KEY (incident_id, event_id)
```

Proposed indexes:

```text
idx_incidents_status_updated       (status, updated_at)
idx_incidents_severity_updated     (severity, updated_at)
idx_incidents_resource_updated     (resource_type, resource_id, updated_at)
idx_incidents_correlation_status   (correlation_key, status)
idx_incident_events_event          (event_id)
```

## 15. Incident correlation rules

The first IncidentEngine should use explicit correlation keys rather than inferred causality.

| Event family | Correlation key | Opening rule | Resolution rule |
|---|---|---|---|
| Container stability | `container:{container_id}:stability` | `ContainerRestarting`, `ContainerRestarted`, `ContainerStopped`, or unhealthy health evidence. | `ContainerRecovered`, healthy health transition, or explicit stable running/healthy evidence after an opening event. |
| Project Git state | `project:{project_path}:git` | Transition into dirty, conflicted, detached, or behind evidence. | Transition back to clean/non-conflicted/non-detached and no behind evidence. |
| Tunnel availability | `tunnel:local:availability` | Transition into `down`. | Transition from `down` to `healthy`. |
| Health state | `project:{project_path}:health` | Transition into `degraded` or `critical`. | Transition to `healthy`. |

An open incident with the same correlation key receives a new event and updates `updated_at`; it does not create a second open incident. A recovery event closes the matching open incident and sets `resolved_at`. A recovery event with no open incident remains an event but does not create a misleading resolved incident.

The first version should not correlate unrelated resources, infer root cause, or group events solely because they occurred near each other. A later milestone may introduce a broader correlation graph after deterministic evidence is available.

## 16. Event and incident retention strategy

Event retention must be independent from high-frequency telemetry retention. The initial proposal is:

| Data | Initial policy proposal | Safety condition |
|---|---:|---|
| Telemetry samples | Existing MC-2 policy, default 1 day. | Unchanged by MC-3. |
| Events | Configurable 30 days after measurement. | Never delete events attached to an open incident. |
| Resolved incidents | Configurable 180 days after measurement. | Never delete open or acknowledged incidents. |
| Incident join rows/evidence | Cascade only with their parent event/incident. | Preserve referential integrity. |

Because expected volume has not yet been measured, MC-3 should create the schema and document the policy first. Cleanup should be implemented only after event volume and database growth are measured with representative resources. There must be no filesystem cleanup outside the same SQLite database.

## 17. API design

Existing routes must remain unchanged:

```text
GET /healthz
GET /api/overview
GET /api/history/host
GET /api/history/containers
GET /api/history/projects
GET /api/history/tunnel
```

Add event endpoints only after event persistence and query behavior are stable:

```text
GET /api/events
GET /api/events/{id}
```

Supported filters should be bounded and typed:

```text
status, severity, event_type, resource_type, resource_id, start, end, limit
```

Add incident endpoints after IncidentRepository and IncidentEngine are stable:

```text
GET /api/incidents
GET /api/incidents/{id}
POST /api/incidents/{id}/acknowledge
```

The acknowledgement endpoint is optional for the first implementation. If included, it performs only an incident-status update inside the incident repository, is idempotent, requires no infrastructure access, and returns a safe structured response. It must not accept or expose operational actions.

Event and incident responses should use typed mappers and expose safe fields only. SQL, internal database paths, tracebacks, tokens, and raw exception text must remain internal.

## 18. Incident Room UI design

The UI should be added only after the domain, repositories, and APIs pass tests. It should preserve Mission Control’s existing dark operational visual language and add a focused Incident Room section rather than redesigning the dashboard.

Initial views:

| View | Content |
|---|---|
| Open incidents | Title, severity, status, resource, started time, duration, latest event. |
| Incident detail | Summary, evidence, event timeline, current status, acknowledgement if approved. |
| Event stream | Bounded recent events with type, severity, resource, timestamp, and transition details. |
| Filters | Status, severity, event type, resource, and time range. |

No action controls should appear other than safe acknowledgement. There must be no restart, stop, start, update, rollback, shell, Docker exec, backup, restore, or Cloudflare action.

## 19. Exact files to create

| File | Responsibility |
|---|---|
| `src/aipm/models/events.py` | Typed `Event`, `EventType`, `EventSource`, `ResourceRef`, `FindingEvidence`, and event filters. |
| `src/aipm/models/incidents.py` | Typed `Incident`, `IncidentStatus`, incident filters, and incident event link models. |
| `src/aipm/repositories/events/__init__.py` | Event repository exports. |
| `src/aipm/repositories/events/base.py` | Event repository protocol and transactional processing interface. |
| `src/aipm/repositories/events/sqlite.py` | Event/evidence/processing-run schema, idempotent writes, and event queries. |
| `src/aipm/repositories/incidents/__init__.py` | Incident repository exports. |
| `src/aipm/repositories/incidents/base.py` | Incident repository protocol. |
| `src/aipm/repositories/incidents/sqlite.py` | Incident/incident-event schema, correlation lookup, status changes, and queries. |
| `src/aipm/services/events/frame.py` | Historical comparison frame reconstruction over MC-2 history. |
| `src/aipm/services/events/derivation.py` | Pure deterministic transition-to-event rules. |
| `src/aipm/services/events/processor.py` | Run-level orchestration, Health Engine evidence collection, idempotency, and transactional persistence. |
| `src/aipm/services/events/runner.py` | Dedicated `aipm events run` polling process with graceful signals. |
| `src/aipm/services/incidents/engine.py` | Explicit event-family correlation, incident creation/update/resolution, and acknowledgement semantics. |
| `src/aipm/services/incidents/query.py` | Typed incident/event query service with safe filtering and limits. |
| `src/aipm/mappers/events.py` | Event domain-to-response mapping. |
| `src/aipm/mappers/incidents.py` | Incident domain-to-response mapping. |
| `src/aipm/capabilities/events/__init__.py` | Event capability package. |
| `src/aipm/capabilities/events/commands.py` | `aipm events process` and `aipm events run` CLI commands. |
| `src/aipm/capabilities/dashboard/incidents_api.py` | Dashboard façade for events/incidents. |
| `tests/test_event_models.py` | Typed model and enum tests. |
| `tests/test_event_derivation.py` | Transition, no-change, restart, recovery, Git, tunnel, and health rules. |
| `tests/test_event_idempotency.py` | Duplicate processing and event-key uniqueness tests. |
| `tests/test_event_repository.py` | Temporary SQLite event/evidence/processing persistence tests. |
| `tests/test_incident_engine.py` | Creation, correlation, resolution, acknowledgement, and no-duplicate tests. |
| `tests/test_incident_api.py` | Safe filtering, API failure isolation, and response contract tests. |
| `docs/MC-3_ARCHITECTURE.md` | Approved event/incident design and correlation rules. |

## 20. Exact files to modify

| File | Proposed change |
|---|---|
| `src/aipm/models/config.py` | Add an optional typed event-processing configuration section, reusing MC-2’s database path. |
| `src/aipm/core/config.py` | Load and validate event processor enablement, polling interval, and deferred independent retention settings. |
| `src/aipm/cli/app.py` | Register `aipm events process` and `aipm events run`. |
| `src/aipm/repositories/telemetry/base.py` | Add only the read/query methods required to obtain source-run comparison frames, or introduce a separate frame reader without moving event SQL into telemetry routes. |
| `src/aipm/repositories/telemetry/sqlite.py` | Add shared read helpers or migration hooks only if necessary; do not mix event records into telemetry write methods. |
| `src/aipm/dashboard/server.py` | Add event/incident routes only after backend verification; preserve all current routes. |
| `src/aipm/dashboard/static/index.html` | Add the focused Incident Room only after API/domain verification; preserve current visual language. |
| `README.md` | Document MC-3 commands and read-only event/incident boundaries. |
| `docs/MISSION_CONTROL.md` | Document event processor systemd template, API, incident acknowledgement, and safety constraints. |
| `pyproject.toml` | Only metadata changes if tests or command registration require them; no AI or external event platform dependencies. |

## 21. Exact files to remove

No files should be removed. MC-3 should extend MC-2 and preserve the existing dashboard, sampler, history repository, APIs, and tests.

## 22. Test strategy

All tests must use temporary SQLite databases or pure in-memory typed fixtures. They must never depend on the production VPS, live Docker, live Git remotes, systemd, Cloudflare, or a production telemetry database.

Required coverage includes:

| Area | Tests |
|---|---|
| Event model | Enum values, typed resources, evidence, and UTC timestamps. |
| Derivation | State transitions, no event when unchanged, start/stop/restarting/restart/recovery, Git changes, tunnel changes, health transitions. |
| Idempotency | Same source run twice, same event key twice, processor retry after failure, no duplicate incident. |
| Health integration | Reuse of HealthEngine/Finding/Severity/HealthState, analyzer failure isolation, no duplicate health taxonomy. |
| Repository | Schema, foreign keys, indexes, normalized event/evidence rows, processing markers, filters, and safe database failure. |
| Incident engine | Creation, correlation, repeated events, recovery/resolution, acknowledgement, and open-incident preservation. |
| History integration | Previous/current frame selection, missing data, unavailable components, new container ID behavior. |
| API | Event/incident filtering, bounded limits, no-data, repository failure, safe errors, and compatibility of all MC-2 routes. |
| Read-only boundary | No Docker/Git/Compose/systemd/Cloudflare mutation strings or calls in processor/derivation/incident layers. |

## 23. Risks and mitigations

| Risk | Mitigation |
|---|---|
| False container lifecycle events after recreation | Compare stable `container_id` only; do not infer identity from names. |
| Duplicate events after processor restart | Unique deterministic `event_key`, unique source-run processing marker, and one transaction. |
| Duplicate incidents during retries | Unique incident/correlation lookup and unique incident-event links. |
| Health Engine output is not persisted by MC-2 | Add normalized health observations in MC-3 and reuse existing HealthEngine/Finding types. |
| Event processor races with sampler | Process only committed source runs; SQLite transaction boundaries and retryable processing markers. |
| Event processor races with another processor | Single systemd process by deployment design plus database uniqueness/idempotency guards. |
| Over-correlation hides distinct incidents | Use explicit family/resource correlation keys; do not correlate by proximity alone. |
| Retention deletes evidence for open incidents | Do not delete events attached to open/acknowledged incidents. Measure volume before enabling long-term cleanup. |
| API/UI added before backend stability | Implement repositories and deterministic tests first; expose routes and UI last. |
| Hidden operational mutation | Keep processor and engine dependent only on typed snapshots, history, and HealthEngine read paths; audit executable layers. |

## 24. Recommended implementation sequence

1. Add and validate event-processing configuration while preserving MC-2 defaults.
2. Add typed Event, Incident, resource-reference, evidence, filter, and status models.
3. Add comparison-frame readers over the existing telemetry history.
4. Add event/evidence/processing-run SQLite schema and repository protocols.
5. Implement pure EventDerivationService with only the reliable initial event set.
6. Add normalized health observations and integrate the existing HealthEngine as evidence.
7. Implement run-level EventProcessor with atomic idempotency and no sampler changes.
8. Implement IncidentEngine with explicit correlation, resolution, and optional acknowledgement.
9. Add the dedicated `aipm events process` and `aipm events run` process; do not start it from FastAPI.
10. Run model, derivation, idempotency, repository, health, incident, and failure-isolation tests.
11. Add safe event/incident query services and APIs while preserving every MC-2 route.
12. Add the focused Incident Room UI only after backend/API verification.
13. Measure event/incident volume and decide whether independent retention cleanup is safe.
14. Update documentation, run `pytest -q`, run `git diff --check`, scan for mutations, and create a separate MC-3 commit.
15. Stop at MC-3. Do not begin MC-4 alerts, MC-5 guarded operations, or MC-6 AI advisor.

## Approval request

This is an assessment only. No MC-3 repository code has been written. Please review the event types, health-observation addition, separate event processor choice, idempotency scheme, incident correlation rules, exact file plan, and milestone boundary. Reply **Approved** before implementation begins.
