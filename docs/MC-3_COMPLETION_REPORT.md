# Mission Control MC-3 Completion Report

## Completion status

MC-3, **Event Engine & Incident Room**, is implemented in the existing `MenaYassa/AIPM` repository on top of the immutable MC-2 commit:

```text
151a855 feat: add Mission Control historical telemetry
```

MC-3 remains deterministic, read-only toward VPS infrastructure, and free of AI, alerts, remediation, and operational controls. It uses the existing MC-2 SQLite telemetry database and preserves the existing Mission Control overview and historical API contracts.

## Architecture implemented

```text
Persisted MC-2 sample_runs and normalized facts
              ↓
HistoricalFrameService
              ↓
Existing HealthEngine + HealthEvidenceService
              ↓
EventDerivationService
              ↓
SQLiteEventRepository
              ↓
IncidentEngine
              ↓
SQLiteIncidentRepository
              ↓
Event/Incident query services
              ↓
Incident Room API and UI
```

The `TelemetrySampler` remains responsible only for collecting the current snapshot, mapping facts, persisting telemetry, and enforcing telemetry retention. MC-3 does not start the event processor from FastAPI. The separate `aipm events run` process polls committed sample runs and processes them once.

The event derivation and event processor layers do not import or directly call Docker, Git, Compose, systemd, Cloudflare, psutil, subprocess, or infrastructure mutation operations. The HealthEngine integration is the existing deterministic analyzer boundary; the event processor consumes its normalized evidence.

## Files created

| File | Responsibility |
|---|---|
| `docs/MC-3_ARCHITECTURE.md` | Approved architecture assessment and correlation design. |
| `docs/MC-3_COMPLETION_REPORT.md` | This completion report. |
| `scripts/measure_event_storage.py` | Actual-schema temporary SQLite event/incident storage measurement. |
| `src/aipm/models/events.py` | Typed event types, sources, resources, evidence, filters, and processing results. |
| `src/aipm/models/incidents.py` | Typed incident statuses, incidents, links, and filters. |
| `src/aipm/models/health_observation.py` | Normalized HealthEngine observation and Finding evidence models. |
| `src/aipm/repositories/events/__init__.py` | Event repository exports. |
| `src/aipm/repositories/events/base.py` | Event repository protocol. |
| `src/aipm/repositories/events/sqlite.py` | Event, evidence, health-observation, processing-marker schema and persistence. |
| `src/aipm/repositories/incidents/__init__.py` | Incident repository exports. |
| `src/aipm/repositories/incidents/base.py` | Incident repository protocol. |
| `src/aipm/repositories/incidents/sqlite.py` | Incident and incident-event schema, correlation updates, acknowledgement, and queries. |
| `src/aipm/services/events/__init__.py` | Event services package. |
| `src/aipm/services/events/frame.py` | Typed adjacent historical-frame reconstruction. |
| `src/aipm/services/events/derivation.py` | Pure deterministic transition rules and stable event keys. |
| `src/aipm/services/events/health.py` | Existing HealthEngine evidence collection and health transition derivation. |
| `src/aipm/services/events/processor.py` | Run-level idempotent event processing. |
| `src/aipm/services/events/query.py` | Bounded event query service. |
| `src/aipm/services/events/runner.py` | Dedicated event polling process with graceful signals. |
| `src/aipm/services/incidents/__init__.py` | Incident services package. |
| `src/aipm/services/incidents/engine.py` | Explicit incident opening, correlation, resolution, and acknowledgement rules. |
| `src/aipm/services/incidents/query.py` | Bounded incident query and acknowledgement service. |
| `src/aipm/capabilities/events/__init__.py` | Event CLI capability exports. |
| `src/aipm/capabilities/events/commands.py` | `aipm events process` and `aipm events run`. |
| `src/aipm/capabilities/dashboard/incidents_api.py` | Safe dashboard event/incident façade. |
| `src/aipm/mappers/events.py` | Event API response mapping. |
| `src/aipm/mappers/incidents.py` | Incident API response mapping with event timelines. |
| `tests/test_event_derivation.py` | State-transition and stable-key tests. |
| `tests/test_event_repository.py` | Temporary SQLite event schema, evidence, filters, and idempotency tests. |
| `tests/test_event_processor.py` | History-to-event-to-incident integration and repeat-processing tests. |
| `tests/test_health_events.py` | Health state and HealthEngine finding transition tests. |
| `tests/test_incident_engine.py` | Incident creation, correlation, resolution, and acknowledgement tests. |
| `tests/test_incident_api.py` | Event/incident API filters, details, acknowledgement, and safe failure tests. |

## Files modified

| File | Change |
|---|---|
| `README.md` | Added MC-3 commands, API, and Incident Room documentation. |
| `config/aipm.yaml` | Added event processor, event retention, incident retention, and acknowledgement defaults. |
| `docs/MISSION_CONTROL.md` | Added MC-3 architecture, commands, systemd template, event rules, APIs, and safety boundaries. |
| `src/aipm/cli/app.py` | Registered `events process` and `events run` without changing existing commands. |
| `src/aipm/core/config.py` | Loads and validates optional MC-3 `events` configuration. |
| `src/aipm/dashboard/server.py` | Added thin event/incident routes while preserving all MC-2 routes. |
| `src/aipm/dashboard/static/index.html` | Added focused Incident Room cards and event timeline rendering. |
| `src/aipm/models/config.py` | Added typed `EventConfig`. |
| `src/aipm/models/history.py` | Added typed historical run references. |
| `src/aipm/repositories/telemetry/base.py` | Added typed run and per-run fact readers. |
| `src/aipm/repositories/telemetry/sqlite.py` | Added typed adjacent-run and per-run history queries. |
| `tests/test_telemetry_config.py` | Added MC-3 event configuration tests. |

## Files removed

No files were removed.

## Event types implemented

| Event type | Source | Derivation rule |
|---|---|---|
| `container_started` | MC-2 telemetry | Same container ID; previous state was neither `running` nor `restarting`, current state is `running`. |
| `container_restarting` | MC-2 telemetry | Same container ID; current state is `restarting` and previous state was not `restarting`. |
| `container_restarted` | MC-2 telemetry | Same container ID; observed `restart_count` increased. |
| `container_stopped` | MC-2 telemetry | Same container ID; previous state was `running`/`restarting`, current state is `exited`/`dead`. |
| `container_recovered` | MC-2 telemetry | Same container ID; `restarting → running`, or health `unhealthy → healthy`. |
| `container_health_changed` | MC-2 telemetry | Same container ID; both health values are present and changed. |
| `project_git_state_changed` | MC-2 telemetry | Same project path/name; branch, dirty, ahead, or behind signature changed. |
| `tunnel_state_changed` | MC-2 telemetry | Known local tunnel state changed; unknown states are ignored. |
| `health_state_changed` | Existing HealthEngine | Persisted normalized project HealthState changed. |
| `health_finding_changed` | Existing HealthEngine | Deterministic Finding fingerprint set changed. |

MC-3 intentionally does not implement threshold events, Compose state events, inferred container lifecycle across different IDs, alerts, causal explanations, or AI-generated events.

## Health Engine integration

MC-3 reuses the existing `HealthEngine`, `GitAnalyzer`, `ComposeAnalyzer`, `DockerAnalyzer`, `Finding`, `Severity`, `HealthState`, and `HealthReport` models. `HealthEvidenceService` runs the existing engine for discovered project paths and stores normalized observations and Finding fingerprints in the same SQLite database.

The Finding fingerprint is based on stable evidence fields:

```text
code + component + resource + severity + title
```

Descriptions and recommendations remain evidence rather than identity inputs. Analyzer failures continue to be normalized by the existing HealthEngine behavior; the event processor returns a safe unavailable result and logs diagnostics through the shared AIPM logger.

## Idempotency behavior

Events use a deterministic SHA-256 key derived from:

```text
previous_run_id + source_run_id + event_type + resource_id + previous_value + current_value
```

The database uses `events.event_key UNIQUE` and `event_processing_runs.source_run_id PRIMARY KEY`. `EventProcessor` checks and atomically inserts the processing marker before writing observations and events. A second attempt against the same source run returns `processed=False` and creates no duplicate event or incident.

The event runner advances its high-watermark only after a source run succeeds. Failed source runs remain retryable. The incident-event join uses a composite primary key, so replaying a persisted event cannot attach it twice.

## Incident correlation rules

Incidents use explicit correlation keys:

| Family | Correlation key | Opens | Resolves |
|---|---|---|---|
| Container stability | `container:{container_id}:stability` | Restarting, restarted, stopped, unhealthy health change. | Container recovered or healthy health change. |
| Project Git | `project:{project_path}:git` | Dirty or behind state. | Clean and not behind. |
| Tunnel availability | `tunnel:local:availability` | Known transition to `down`. | Known transition to `healthy`. |
| Project health | `project:{project_path}:health` | Degraded or critical HealthState. | Healthy HealthState. |

An existing open or acknowledged incident with the same correlation key is updated rather than duplicated. A new opening event reopens an acknowledged incident as `open`. A recovery with no matching open incident remains an event and does not create a misleading resolved incident.

Acknowledgement changes only the incident status from `open` to `acknowledged`. It does not execute restart, stop, start, update, rollback, shell, Docker exec, backup, restore, or any other infrastructure operation.

## Database schema

MC-3 reuses the MC-2 telemetry database and adds the following normalized tables:

| Table | Purpose |
|---|---|
| `event_processing_runs` | One idempotent processing marker per committed telemetry run. |
| `health_observations` | One HealthEngine report summary per project and source run. |
| `health_findings` | Normalized Finding evidence linked to a health observation. |
| `events` | Typed deterministic event records with source/resource/transition fields. |
| `event_evidence` | Finding evidence attached to events. |
| `incidents` | Correlated incident state and lifecycle timestamps. |
| `incident_events` | Many-to-many event timeline links with a composite uniqueness key. |

The new tables use foreign keys to MC-2 `sample_runs` or the relevant MC-3 parent, parameterized SQL, WAL where supported, and indexes for timestamp, resource, correlation, event type, incident status, and incident-event lookups. No second database was created and no MC-2 telemetry table was repurposed for events.

## CLI and API endpoints

The new CLI commands are:

```text
aipm events process
aipm events process --run-id 42
aipm events run
```

Existing commands, including `aipm telemetry sample`, `aipm telemetry run`, and `aipm dashboard`, remain unchanged.

The new safe API routes are:

```text
GET /api/events
GET /api/events/{id}
GET /api/incidents
GET /api/incidents/{id}
POST /api/incidents/{id}/acknowledge
```

Event filters include `range`, `severity`, `event_type`, `resource_type`, `resource_id`, and bounded `limit`. Incident filters include `range`, `status`, `severity`, `resource_id`, and bounded `limit`. API errors return structured unavailable/not-found responses without exposing SQL, tracebacks, database paths, or infrastructure credentials.

The following MC-2 routes were preserved:

```text
GET /healthz
GET /api/overview
GET /api/history/host
GET /api/history/containers
GET /api/history/projects
GET /api/history/tunnel
```

## UI changes

The existing Mission Control design was preserved. A focused **Incident Room** section was added below the current telemetry and handbook layout. It displays open incidents, severity, status, resource, start time, summary, and up to the latest five event timeline entries.

The UI is read-only except for the optional acknowledgement endpoint. It does not show or invoke remediation controls. It gracefully shows an unavailable state if the event/incident database is disabled or unavailable.

## Tests and verification

The final suite result is:

```text
69 passed, 1 warning
```

The warning is the existing Starlette/httpx test-client deprecation warning from the installed dependency stack.

Coverage includes typed event/incident models, all implemented transition types, no-change behavior, restarting/recovery, restart counters, Git state, tunnel state, HealthState transitions, Finding changes, event repository schema and filters, processing markers, event-key idempotency, history-to-event processing, incident creation/correlation/resolution, acknowledgement, safe API failures, MC-2 API compatibility, configuration validation, temporary SQLite databases, and CLI/API smoke tests.

The direct event runner was verified with a temporary database and SIGTERM; it exited with code 0. `aipm events process` was verified against a temporary sample run. The live local server returned healthy `/healthz`, available empty event/incident responses, and served the Incident Room marker.

`git diff --check` passed.

## Storage measurement

`scripts/measure_event_storage.py` used the actual MC-3 schema in a temporary SQLite database and inserted 120 representative event-processing cycles, one event per cycle, and one open correlated incident with event links.

| Measurement | Result |
|---|---:|
| Representative cycles | 120 |
| Event rows | 120 |
| Open incidents | 1 |
| Database size before samples | 155,648 bytes |
| Database size after samples | 204,800 bytes |
| Measured growth | 49,152 bytes |
| Measured growth per event cycle | 409.6 bytes |
| Projected 24-hour growth at 15-second cycles | 2,359,296 bytes |
| Projected 30-day growth at 15-second cycles | 70,778,880 bytes |

The measurement is a schema-based baseline, not a production estimate. It includes event indexes, evidence/processing tables, and one correlated incident. Event retention should remain independently configurable and should not delete events attached to open or acknowledged incidents. No long-term cleanup job was enabled by MC-3.

## Safety and security audit

The mutation audit scanned MC-3 event, incident, repository, capability, mapper, API, and server Python layers for Git fetch/pull/checkout/stash/reset/clean, Docker start/stop/restart/rm/prune/exec, Compose up/down/pull, systemd start/stop/restart/enable/disable, Cloudflare mutation, subprocess, package installation, filesystem deletion, and shell execution patterns.

The audit found no forbidden operations. Event derivation and processing contain no direct infrastructure-provider imports. FastAPI does not start the event processor. No production VPS, Docker, Compose, Git project, systemd unit, package set, Cloudflare configuration, firewall, or production database was modified.

## Known limitations

MC-3 does not infer container lifecycle across different container IDs. A container recreation is therefore not treated as a stop/start/restart transition unless the same ID provides adjacent facts.

The event runner processes committed runs in one dedicated process. The implementation provides database uniqueness and retry-safe markers, but production should still deploy one event runner instance per telemetry database.

The first history-frame implementation compares adjacent sample runs. If a telemetry run is unavailable or a resource row is missing, it emits no transition rather than inventing one.

Health observations are generated for project paths that can be rediscovered through the configured `ProjectService`. A project removed from discovery will not receive a fabricated health recovery or failure event.

The event and incident retention settings are validated and documented, but long-term cleanup is intentionally not automatically enabled until production event volume is measured. The storage script uses representative temporary data and must be rerun with actual VPS resource counts before selecting retention.

The Incident Room currently shows open incidents and their latest event timeline but does not yet support full event exploration, pagination UI, incident notes, or notifications.

No alerting, AI root-cause analysis, embeddings, RAG, AI recommendations, or operational controls are included.

## Recommended next milestone

The next milestone should be **MC-4 Alerts & Notifications**, but it requires explicit approval. It should consume deterministic events/incidents only and add notification policy, delivery adapters, deduplication windows, rate limits, and audit records without adding remediation controls or AI. MC-5 guarded operations and MC-6 AI advisor must remain separate milestones.

MC-3 is complete and should stop here.
