# MC-6.7 Design — Allow-Listed Systemd Observation

**Status:** Design and inspection only

**Baseline:** `f55397374cb4a8648e47bd49d9afcf591924369f`

**Implementation status:** Not started

## 1. Exact roadmap objective

The authoritative MC-6 implementation plan defines **MC-6.7 as a NEW milestone for an allow-listed Systemd observation façade/API and page**. The required posture is read-only only, with no unit mutation. The implementation sequence is to define an allow-listed unit registry, define a provider protocol for structured unit state, implement a local adapter with bounded command arguments and safe parsing, add a capability façade and GET routes, add Web UI and TUI renderers, and add static mutation guards with fake-manager tests.

The existing API gap analysis proposes the additive routes:

```text
GET /api/systemd/units
GET /api/systemd/units/{unit_name}
```

The first implementation must be limited to known AIPM units and explicitly configured safe units rather than exposing every unit on the host.

## 2. Reconciliation with current MC-6 architecture

MC-6.1 through MC-6.6.3 established the shared Observation and freshness vocabulary, bounded query conventions, secret-safe mappers, the vanilla shell and scheduler, Server intelligence, Docker intelligence, project/application intelligence, and the refined Projects UX. Those capabilities are completed and must remain unchanged in meaning.

MC-6.7 is not already implemented as a general capability. The repository has only narrow systemd awareness in the tunnel telemetry path: the cloudflared fallback performs a fixed, failure-isolated `systemctl is-active cloudflared` observation. The service-health façade reports telemetry and MC-3 freshness, but does not inventory units or expose unit detail. Static handbook content contains systemd command guidance, but handbook text is not a provider or observation API. Therefore, MC-6.7 is **not obsolete**, but it must generalize existing bounded knowledge without duplicating or bypassing the current tunnel telemetry seam.

The Systemd page is currently a placeholder in the frontend navigation. Replacing that placeholder would be the narrow vertical slice for MC-6.7. Existing Server, Docker, Project, History, Incident, Notification, and Settings pages are not to be redesigned as part of this milestone.

## 3. EXISTS / EXTEND / NEW / UNAVAILABLE classification

| Area | Classification | Design conclusion |
|---|---|---|
| Shared Observation state and freshness | EXISTS | Reuse `Observation`, `ObservationState`, availability, freshness, and safe error semantics. |
| Bounded query validation | EXISTS | Reuse query-bound helpers; add only unit-name and dependency-depth validation if required. |
| Response/output safety scanner | EXISTS | Reuse secret-safe scanner and allow-listed mapper policy. |
| FastAPI dashboard adapter | EXISTS / EXTEND | Add two additive GET routes without changing existing routes. |
| Vanilla shell/router/sidebar | EXISTS / EXTEND | Replace only the Systemd placeholder and register one scheduler resource. |
| Centralized frontend scheduler | EXISTS / EXTEND | Add one bounded Systemd resource cadence; do not create a second timer. |
| Tunnel-specific systemd check | EXISTS | Preserve it as a narrow existing source; do not make the tunnel service depend on the new page during the first slice unless composition is proven safe. |
| General unit registry | NEW | Define a backend-owned allow-list of safe unit identifiers. |
| Structured Systemd provider protocol | NEW | Introduce an adapter interface returning normalized unit state, not raw command output. |
| Local Systemd observation adapter | NEW | Use fixed, bounded, read-only manager queries with safe parsing and failure isolation. |
| Dashboard Systemd façade | NEW | Provide bounded list/detail methods and explicit unavailable/error envelopes. |
| Systemd domain models and mapper | NEW | Add typed unit snapshots, detail, status enums, evidence, and safe errors. |
| Systemd history | UNAVAILABLE for first slice | Do not add a database or historical writer. Current state is sufficient for MC-6.7; history remains a later extension using existing history infrastructure if approved. |
| TUI Systemd renderer | EXTEND / DEFERRED | Define the shared contract now; implement the TUI panel only if MC-6.7 scope explicitly includes the already-planned TUI slice, otherwise consume the contract in MC-6.11. |
| Logs, incidents, operations, authentication | UNAVAILABLE / OUT OF SCOPE | Do not expand MC-6.7 into MC-6.8, MC-6.9, MC-6.12, or public access work. |

## 4. Existing reusable data sources and components

The implementation must reuse the following repository seams:

| Existing component | Reuse in MC-6.7 |
|---|---|
| `src/aipm/models/mission_control.py` | Observation state, freshness, transport, availability, and semantic error contract. |
| `src/aipm/capabilities/dashboard/query_bounds.py` | Bounded list/detail query validation. |
| `src/aipm/capabilities/dashboard/safety.py` | Secret/output safety scanning and safe payload assertions. |
| `src/aipm/dashboard/server.py` | FastAPI adapter, static mount, dependency construction, and additive route wiring. |
| `src/aipm/dashboard/static/mission-control-shell.js` | Hash routing, navigation registration, sidebar behavior, and placeholder replacement. |
| `src/aipm/dashboard/static/mission-control-scheduler.js` | One resource timer, visibility behavior, cancellation, and polling cadence. |
| `src/aipm/dashboard/static/mission-control-state.js` | Normalized observation state and UI state classes. |
| `src/aipm/capabilities/dashboard/service_health_api.py` | Existing service-health composition patterns, without pretending it is a unit inventory. |
| `src/aipm/services/telemetry/tunnel.py` | Existing narrow cloudflared systemd fallback; preserve behavior and isolate any shared helper extraction. |
| `src/aipm/services/system/service.py` | Existing host/system composition style only. It is not sufficient for unit observation and must not be expanded with arbitrary manager commands. |
| Existing `Application` composition | Construct the new provider/service/façade through the established application boundary. |
| Existing frontend card/table conventions | Render bounded unit rows and detail panels consistently with Server, Docker, and Project pages. |
| Existing tests and fake-provider patterns | Prove unavailable states, safe parsing, bounds, no mutation, and route contracts without a live manager. |

No existing Systemd repository, database, worker, writer, or general provider is present. MC-6.7 must not create a second telemetry or persistence mechanism.

## 5. What exists versus what MC-6.7 adds

The current repository already knows how to expose safe read-only observations through typed models, façades, mappers, FastAPI routes, and shared frontend scheduling. It also contains one fixed cloudflared activity check. Those are reusable patterns, not a complete Systemd feature.

MC-6.7 must add only the missing general capability: a backend-owned allow-list, a normalized provider protocol, a bounded local adapter, typed unit contracts, a dashboard façade, two GET routes, a Systemd page, and focused tests. It must not add arbitrary unit selection, raw manager output, mutation verbs, process inspection, logs, or a persistence layer.

## 6. Proposed data flow and architecture

```text
Browser
  │
  ├── GET /api/systemd/units?limit=...
  └── GET /api/systemd/units/{opaque_allowlisted_unit_id}
        │
FastAPI dashboard adapter
        │
DashboardSystemdApi
        │
SystemdObservationService
        │
SystemdProvider protocol
        │
Bounded local Systemd adapter
        │
User/system manager query with fixed arguments
        │
Normalized SystemdUnitSnapshot / SystemdUnitDetail
        │
Safe mapper + Observation envelope
        │
Vanilla Systemd page and shared scheduler
```

The browser supplies only bounded query values and an opaque allow-listed identifier. The adapter resolves that identifier against a backend-owned registry and constructs fixed manager queries. The provider returns structured fields; it does not return raw stdout, raw environment values, full unit files, arbitrary command lines, or unbounded dependency graphs.

A provider failure is isolated per observation. The service returns an explicit unavailable or error state rather than inferring a unit as healthy from missing data. A unit outside the registry is rejected before any manager query.

## 7. Proposed additive API contracts

### `GET /api/systemd/units`

The list route returns a bounded envelope containing the observation metadata, the configured allow-list projection, and normalized unit summaries. The initial query should accept only a small positive `limit` within the shared query bounds. No browser-provided unit name is passed to a command.

Illustrative response shape:

```json
{
  "observation": {
    "state": "fresh",
    "available": true,
    "transport_ok": true,
    "observed_at": "2026-08-18T12:00:00Z",
    "age_seconds": 3
  },
  "units": [
    {
      "id": "aipm-dashboard",
      "display_name": "AIPM Dashboard",
      "load_state": "loaded",
      "active_state": "active",
      "sub_state": "running",
      "enabled": false,
      "main_pid": null,
      "health": "active",
      "evidence": []
    }
  ],
  "errors": []
}
```

The actual field names must use the repository’s typed naming conventions. `main_pid` should be omitted or null unless it is demonstrably needed and safe. Raw `ExecStart`, environment, drop-in content, invocation IDs, cgroup paths, host paths, and arbitrary unit properties are excluded.

### `GET /api/systemd/units/{unit_id}`

The detail route accepts only an opaque registry identifier. It may return bounded dependency and ordering summaries for the allow-listed unit, with a maximum depth and maximum item count. It must not accept an arbitrary unit string from the browser, expose all manager properties, or return unit-file contents.

Both routes remain additive. Existing `/api/overview`, `/api/services`, `/api/server`, `/api/docker/*`, `/api/projects*`, history, events, incidents, notifications, and other MC-5/MC-6 routes remain unchanged.

## 8. Domain and model contracts

Proposed new typed contracts are:

- `SystemdUnitId`: an opaque, backend-owned stable identifier.
- `SystemdUnitRegistryEntry`: internal allow-list metadata containing the safe manager unit name, display label, scope, and permitted detail fields.
- `SystemdUnitState`: normalized `load_state`, `active_state`, `sub_state`, enablement posture, safe timestamps, and bounded health classification.
- `SystemdUnitSnapshot`: list-level normalized state with Observation metadata.
- `SystemdUnitDetail`: snapshot plus bounded dependency/order summaries and evidence.
- `SystemdUnitStatus`: safe enum values such as `active`, `inactive`, `failed`, `activating`, `deactivating`, `unknown`, and `unavailable`.
- `SystemdObservationError`: safe error category and public message, with private adapter details excluded.

The models should use the existing Observation semantics rather than inventing a second freshness vocabulary. Unknown, unavailable, stale, never-sampled, and semantic error states remain distinct.

## 9. Observation, freshness, and error semantics

A successful manager query with normalized state produces a fresh observation. A successful query returning a unit that is inactive or failed is still transport-successful and available; the unit status is unhealthy or degraded, not an Observation transport error.

A manager unavailable, permission-denied, timeout, malformed response, or unsupported query produces an explicit unavailable/error observation. A semantic unit state such as `failed` must not be converted into `unavailable`. A stale cached observation, if caching is introduced later, must be marked stale and must not be silently presented as current.

The first implementation should avoid a separate cache and query the manager on the shared bounded scheduler cadence. If the manager is unavailable, the page must show source-specific unavailability and preserve the last state only if the shared Observation contract explicitly marks it stale.

## 10. Frontend information architecture and UX

Replace the current Systemd placeholder with a read-only **Systemd Observation** page. The page should contain:

1. An observation header showing freshness, availability, and source status.
2. A bounded allow-listed unit table with display name, active state, sub-state, load state, enablement posture, and a safe health/status badge.
3. A detail panel showing only approved fields, safe timestamps, bounded dependency/order summaries, and evidence explaining unavailable or degraded state.
4. Empty, unavailable, stale, never-sampled, error, and no-allow-listed-units states.
5. A clear statement that the page observes units and provides no start, stop, restart, reload, enable, disable, reset-failed, edit, or acknowledgement controls.

Navigation must use the existing hash route and shell registry. Existing Project, Docker, Server, Dashboard, History, Incident, Notification, and Settings UX must not be displaced.

The Systemd page should not display raw command output, `ExecStart`, environment variables, secret-bearing unit properties, arbitrary paths, cgroup internals, or a host-wide inventory. Unit display names should be backend-owned labels rather than raw unit names where possible.

## 11. Polling and scheduling strategy

Register one resource, for example `systemd-units`, with the existing centralized scheduler. The initial cadence should be conservative, such as 30 seconds, matching other operational posture pages. The scheduler must deduplicate timers, pause or defer safely when the page is hidden, prevent overlapping requests, and cancel stale requests on navigation.

No second polling loop, worker, event stream, WebSocket, SSE channel, or background Systemd monitor is introduced. Systemd history is not persisted by this milestone. A later history extension must reuse the existing telemetry/history architecture and pass the active-WAL read-only gate.

## 12. Read-only and safety boundaries

The Systemd provider must be structurally observation-only. It may use fixed, bounded, read-only manager queries such as an allow-listed `systemctl show` or `systemctl list-units` invocation, subject to local platform validation and safe argument construction. It must never accept arbitrary command arguments from the browser.

The following are forbidden in MC-6.7:

- `enable`, `disable`, `start`, `stop`, `restart`, `reload`, `reset-failed`, daemon reload, unit-file installation, or any other mutation verb.
- Arbitrary unit names, arbitrary paths, arbitrary manager properties, or raw unit-file retrieval.
- Shell interpretation, pipelines, command concatenation, `shell=True`, or arbitrary subprocess execution.
- Process enumeration, command-line inspection, environment inspection, or a general process detector.
- Docker lifecycle operations, Compose mutation, Git mutation, notification activation, credentials, Cloudflare, public ingress, or live database writes.

The dashboard remains loopback-only. The existing `read_only=True`, SQLite `mode=ro`, `PRAGMA query_only=ON`, filesystem write-denial, systemd hardening, and no-`CapabilityBoundingSet=` service assumptions remain untouched.

## 13. SQLite and database impact

MC-6.7 requires **no SQLite schema, repository, migration, WAL, or sidecar change**. Current Systemd state is an external observation and should not be written to the telemetry database by the first implementation. No second database or worker is introduced.

If future historical Systemd observations are requested, they must be evaluated as an MC-6.9/history extension using the existing read/write telemetry ownership model, not by making the dashboard a writer. Dashboard reads must continue to use the validated read-only repository boundary.

## 14. Systemd, process, and shell boundaries

Systemd is the subject of observation, not a control plane. The adapter boundary must contain the only manager-query implementation and must expose a narrow provider protocol to the service. The service and façade never construct commands directly.

Process detection is explicitly out of scope. A unit’s `MainPID` may be omitted or included only as a bounded numeric field if the design review proves it is necessary; the page must not use it to inspect processes, command lines, or environments.

Shell execution is prohibited. If the chosen local adapter requires a subprocess mechanism, it must use a fixed executable, fixed argument templates derived from the internal registry, a short timeout, bounded output, no shell, and explicit safe parsing. The implementation must fail closed when the mechanism is unavailable or unsafe. No real Systemd operation is authorized during design or local implementation.

## 15. Docker, provider, credential, and Cloudflare boundaries

MC-6.7 does not add Docker access. Existing Docker intelligence remains authoritative for container state, while Systemd intelligence observes only the allow-listed manager units. No Docker lifecycle method is reachable from the Systemd façade.

No provider credentials, notification configuration, Cloudflare API, public ingress, remote manager, or external network call is needed. The systemd adapter is local-only and should not contact a cloud service. Credential-like fields and provider payloads are excluded from models and mappers.

## 16. Output and secret safety

The mapper must use an explicit allow-list of safe fields. It must redact or omit:

- Environment variables and values.
- `ExecStart`, `ExecReload`, and command-line arguments.
- Unit-file contents and arbitrary drop-ins.
- Credential paths, tokens, URLs containing secret material, authorization data, and destinations.
- Unbounded dependency graphs or private filesystem paths.
- Raw stdout/stderr and exception strings.

Safe public errors must be selected from known categories. Unknown exceptions must map to a generic safe message. Response scans must fail closed on secret-like keys, secret-like values, external URLs where not explicitly reviewed, raw command output, and unexpected provider fields.

## 17. Testing strategy

The implementation gate must include:

| Layer | Required tests |
|---|---|
| Registry/model | Only configured IDs resolve; unknown IDs fail closed; enums and bounded fields are stable. |
| Fake provider | Active, inactive, failed, loading, unavailable, timeout, permission, malformed, and semantic-error states. |
| Adapter | Fixed arguments, no shell, bounded timeout/output, no arbitrary unit input, safe parsing, and failure isolation. |
| Service/façade | Only allow-listed units are queried; detail depth/count bounds hold; no lifecycle provider methods are reachable. |
| API | Both routes are GET-only; route bounds and opaque IDs are enforced; stable JSON envelopes and safe errors. |
| Output safety | No environment, command line, secret, external destination, raw output, or private path reaches responses. |
| Frontend | Systemd page, navigation, `/static` imports, scheduler registration, fresh/stale/unavailable/never-sampled/error/empty states, responsive layout, and absence of action controls. |
| Regression | MC-5, MC-6.1 through MC-6.6.3, read-only SQLite, active-WAL, Docker, project, history, event, incident, and notification suites. |
| Integration | Fake manager only; no real VPS manager, Docker daemon, provider, credential, live SQLite, or network access. |

Static scans must reject mutation verbs, arbitrary subprocess/shell patterns, process detection, credential access, and route methods other than GET. The existing Gate 2.1 harness must remain byte-identical.

## 18. Deployment and rollback considerations

MC-6.7 should first be validated locally with a fake manager and temporary configuration. No target-VPS or live-manager inspection is part of design or implementation approval by itself.

If a later staging approval is granted, use a SHA-verified operator procedure, loopback-only temporary process, fake or isolated manager fixture where possible, no permanent unit installation, and automatic cleanup. The procedure must verify that no lifecycle verb is invoked, the dashboard remains loopback-only, and existing telemetry/MC-3/notification state is unchanged.

Production deployment remains a separate approval gate. It would use the already-reviewed user-level dashboard template and existing read-only filesystem boundary; MC-6.7 must not alter that template or install a new Systemd unit merely to observe Systemd.

Rollback is repository-level revert of the MC-6.7 commit. If deployed, remove only the MC-6.7 static/API capability introduced by the milestone and restore the previous dashboard commit. No rollback may stop, disable, reload, or otherwise mutate observed production units as part of ordinary feature rollback.

## 19. Explicit non-goals

MC-6.7 does not include Systemd start/stop/restart/reload/enable/disable/reset-failed operations, unit-file editing, daemon reload, arbitrary manager commands, process inspection, command-line/environment collection, Logs, Incident/history expansion, notification activation, Git/Docker operations, authentication, authorization, public ingress, Cloudflare changes, AI Agent behavior, action approval, rollback control plane, or MC-6.8 implementation.

It also does not create a second database, Systemd worker, telemetry sampler, event processor, notification worker, or TUI implementation beyond any shared contract needed for future MC-6.11 reuse.

## 20. Risks and mitigations

| Risk | Mitigation |
|---|---|
| `systemctl` availability differs between user and system managers | Make manager scope explicit in the registry; detect unavailable manager safely; do not fall back to arbitrary commands. |
| Unit names or properties contain secrets | Use registry IDs and field allow-lists; omit command/environment/unit-file fields. |
| A failed unit is incorrectly marked unavailable | Separate transport/availability from semantic active state and preserve `failed` as an available degraded observation. |
| Manager output changes across systemd versions | Use bounded key/value parsing with required-field validation and explicit unknown states. |
| Unit inventory grows without bound | Backend-owned allow-list and hard list/detail/dependency limits. |
| Detail dependencies reveal private topology | Return only bounded names/status categories approved by the registry; cap depth and count. |
| Subprocess implementation becomes a shell escape hatch | Fixed executable/arguments, no shell, short timeout, adapter-only ownership, static scans, and fake-manager tests. |
| Systemd page duplicates existing tunnel observation | Preserve the tunnel seam and define a deliberate composition rule; do not replace it casually. |
| Frontend adds polling storms | One centralized scheduler resource with visibility and overlap tests. |
| Read-only deployment assumptions regress | No systemd template or SQLite boundary changes; rerun MC-5.1.x and staging safety gates. |

## 21. Exact future implementation files

Expected files for a later approved implementation are:

```text
docs/MC-6.7_DESIGN.md                 # this design document; no implementation change
aipm models file, if required            # e.g. src/aipm/models/systemd.py
src/aipm/providers/systemd/__init__.py
src/aipm/providers/systemd/protocol.py
src/aipm/providers/systemd/local.py
src/aipm/services/systemd/__init__.py
src/aipm/services/systemd/observation.py
src/aipm/capabilities/dashboard/systemd_api.py
src/aipm/mappers/systemd.py
src/aipm/dashboard/server.py
src/aipm/dashboard/static/index.html
src/aipm/dashboard/static/mission-control-systemd.js
src/aipm/dashboard/static/mission-control-shell.js
src/aipm/dashboard/static/mission-control-scheduler.js

tests/test_mc67_systemd_provider.py
tests/test_mc67_systemd_api.py
tests/test_mc67_systemd_frontend.py
tests/test_mc67_systemd_safety.py
```

The exact model filename and provider package layout should be confirmed against current repository naming conventions before implementation. No listed source or test file is modified by this design-only phase.

## 22. Recommended implementation sequence

1. Confirm the allow-listed unit registry and manager scope using repository review only.
2. Add typed Systemd contracts and safe enums without connecting to a live manager.
3. Add a fake-provider protocol and deterministic fake-manager fixtures.
4. Implement the bounded local adapter with fixed arguments, no shell, timeout, output limits, and safe parsing.
5. Implement the observation service and safe mapper with shared Observation semantics.
6. Implement the dashboard façade and additive GET routes with opaque IDs and bounds.
7. Replace only the Systemd placeholder with the read-only page and register one scheduler resource.
8. Run focused model/provider/API/frontend/safety tests, then MC-5 through MC-6.6.3 regressions and the full suite.
9. Run compilation, JavaScript checks, diff checks, mutation/lifecycle scans, secret/output scans, production-scope scans, and harness identity verification.
10. Stop for review before any commit, target-VPS manager query, persistent deployment, or MC-6.8 work.

## 23. Explicit stop condition before MC-6.8

MC-6.7 is complete only when the allow-listed Systemd observation contract, fake/local adapter, bounded GET façade, Systemd page, scheduler resource, safety tests, and regression gates have been reviewed and approved. After that review, stop. Do not begin MC-6.8 Logs until a separate user instruction explicitly authorizes its design or implementation.

## Safety invariants preserved

The design preserves all existing invariants:

- Dashboard SQLite repositories remain `read_only=True`.
- SQLite read-only connections use URI `mode=ro` and `PRAGMA query_only=ON`.
- The filesystem write-denial boundary remains mandatory and fail-closed.
- Existing systemd service hardening and loopback-only binding remain unchanged.
- Docker lifecycle methods remain unreachable from read façades.
- Shell, process, credential, provider, notification, Cloudflare, and public-ingress operations remain outside scope.
- Existing MC-5 and MC-6 routes remain backward-compatible.
- The Gate 2.1 harness remains unchanged.

## References

[1]: ../PRODUCTION_ROADMAP.md "Repository production roadmap"
[2]: MC-6_IMPLEMENTATION_PLAN.md "MC-6 implementation plan"
[3]: MC-6_ARCHITECTURE.md "MC-6 architecture decisions"
[4]: MC-6_API_GAP_ANALYSIS.md "MC-6 API gap analysis"
[5]: MC-6_UI_SPECIFICATION.md "MC-6 UI and navigation specification"
[6]: ../src/aipm/services/telemetry/tunnel.py "Existing bounded cloudflared Systemd observation seam"
[7]: ../src/aipm/services/system/service.py "Existing system service boundary"
[8]: ../src/aipm/dashboard/server.py "FastAPI dashboard adapter"
[9]: ../src/aipm/models/mission_control.py "Shared Observation and freshness contracts"
[10]: ../ops/systemd/aipm-dashboard.service "Validated dashboard service template"
