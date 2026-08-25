# AIPM Mission Control MC-6 Implementation Plan

## Scope and delivery rule

This document is the authoritative Mission Control roadmap. MC-6.1 through MC-6.8 and MC-6.13 Phases 2, 3, 4A, 4B, the fixture-only 4C presentation, and 4D have been implemented, reviewed, validated, committed, and pushed. The current checkpoint is `e8f0b12d7473e3c021c536e738c8b3a414d116ad`.

MC-6.9 through MC-6.12, MC-6.13 Phase 4B.1, live Phase 4C orchestration, and Phase 4E remain future milestones. MC-6.13 Phase 4B is complete as a private authenticated read-only API boundary, Phase 4C is complete only as a fixture-driven non-live presentation on the existing `#/ai-agent` route, and Phase 4D is complete as a private-VPS telemetry-owned bounded export plus transport-neutral observation adapter ending at `AdvisorCompositionRequest`.

## Milestone map

| Milestone | Classification | Outcome | Current status | Write/action posture |
|---|---|---|---|---|
| MC-6.1 | NEW/EXTEND | Shared contracts, page registry, query bounds, state semantics, and scheduler. | Complete | Read-only only. |
| MC-6.2 | EXTEND | Static frontend shell and navigation extraction. | Complete | Read-only only. |
| MC-6.3 | EXTEND | Dashboard, Server, History, Incidents, and Notifications pages using existing APIs. | Complete | Read-only only. |
| MC-6.4 | EXTEND/NEW | Server detail and capacity façade/API. | Reconciled; already delivered through MC-6.3 | Read-only only. |
| MC-6.5 | EXTEND/NEW | Docker/container/project detail façades and APIs. | Complete | Read-only only. |
| MC-6.6 | EXTEND | Project detail, Git posture, health, and runtime associations. | Complete, including 6.6.1–6.6.3 refinements | Read-only only. |
| MC-6.7 | NEW | Allow-listed Systemd observation façade/API and page. | Complete, including 6.7.1 registry reconciliation | Read-only only; no unit mutation. |
| MC-6.8 | NEW | Bounded, redacted Logs façade/API and page. | Complete and pushed at `d1f6929` | Read-only only; no shell/arbitrary paths. |
| MC-6.9 | EXTEND | Incident/history evidence, comparison queries, cursor pagination, and cross-links. | Next: design/inspection only | Read-only only. |
| MC-6.10 | EXTEND | Settings posture and expanded notification safety/audit views. | Planned | Read-only only; notifications remain disabled. |
| MC-6.11 | NEW | Shared Typer/Rich TUI consuming the same façades and contracts. | Planned | Read-only only. |
| MC-6.12 | FUTURE | Authentication, authorization, approval, action execution, and rollback control plane. | Future, separate gate | Separate authorization required. |
| MC-6.13 Phase 2/3 | EXTEND/NEW | Immutable evidence normalization and ten deterministic, evidence-linked advisor rules. | Complete and pushed at `a7ee2f1` | Pure domain logic only. |
| MC-6.13 Phase 4A | EXTEND/NEW | Bounded immutable request contract and direct normalizer-to-rule-engine composition. | Complete and pushed at `37d8a0e` | No API, UI, LLM, runtime, scheduler, or action behavior. |
| MC-6.13 Phase 4B | EXTEND/NEW | Private authenticated read-only `POST /api/advisor/evaluate` transport boundary over Phase 4A. | Complete and pushed at `af1a10b` | Bounded transport only; no public exposure, UI, live collection, LLM, runtime, scheduler, or action behavior; Phase 4B.1, 4C, 4D, and 4E are separate slices, with 4D now landed and 4B.1/4C/4E remaining separately gated. |
| MC-6.13 Phase 4D | EXTEND/NEW | Telemetry-owned bounded snapshot/export and transport-neutral adapter for the private-VPS CPU, memory, and disk slice, producing canonical observations and `ResourceHistoryEnvelope` values and stopping at `AdvisorCompositionRequest`. | Complete and pushed at `f0ae4bb` and `d90d32f` | No dashboard/UI, live polling, LLM/provider, advisor evaluation, action, approval, or runtime-control behavior; Phase 4D remains separate from Phase 4B.1, 4C, and 4E. |

## MC-6.1 — contracts and UI foundation

### Objectives

MC-6.1 establishes the minimum shared vocabulary before adding pages:

- `ObservationState` and freshness semantics shared by Web UI and TUI.
- Bounded query objects for ranges, limits, cursors, and filters.
- Safe error and availability envelopes.
- Page and resource registry for client scheduling.
- Capability protocol interfaces for future TUI use.
- Response redaction policy and secret-scan fixtures.
- Snapshot/contract fixtures derived from current APIs.

### Proposed implementation areas

These are proposed paths, not files to create in this design-only task:

```text
src/aipm/models/mission_control.py
src/aipm/capabilities/dashboard/contracts.py
src/aipm/capabilities/dashboard/query_bounds.py
src/aipm/capabilities/dashboard/safety.py
src/aipm/dashboard/static/mission-control-state.js
src/aipm/dashboard/static/mission-control-scheduler.js
```

The implementation must first prove that these abstractions reduce duplication. If they do not, retain existing typed models and add only the smallest needed helpers.

### Tests and gate

- Current MC-5 API contract tests remain green.
- State classification tests cover fresh, stale, unavailable, never sampled, and unknown.
- Query bounds reject excessive ranges and limits.
- Secret scanner rejects credential-like keys, values, URLs, destinations, and provider payloads.
- Scheduler tests prove one timer per resource, no overlap, and visibility behavior.
- No application source is deployed until local tests and scope review pass.

## MC-6.2 — frontend shell migration

### Objectives

Extract the current single-file frontend without changing its visible behavior or route contracts. Introduce navigation and component boundaries incrementally.

### Sequence

1. Capture current HTML/API fixtures and browser acceptance baselines.
2. Extract CSS variables, layout primitives, escape/redaction helpers, and render-state helpers.
3. Extract the polling scheduler and resource loaders.
4. Add section registry and navigation anchors.
5. Preserve all current MC-5 sections and empty/error states.
6. Add direct section navigation only after responsive tests pass.

### Gate

No framework migration is allowed in this milestone. A React/Vite evaluation remains a later decision after measured UI complexity.

## MC-6.3 — existing-domain cockpit pages

### Objectives

Turn existing MC-5 sections into navigable Dashboard, History, Incidents, and Notifications pages while reusing current APIs unchanged.

### Required behavior

- Overview and service pulse remain the landing summary.
- History uses existing routes and preserves sparse/freshness semantics.
- Incident Room contains no acknowledgement/action controls.
- Notification Safety exposes safe metrics and metadata only.
- No new database access path is introduced.

### Gate

Run the MC-5 focused tests, full suite, browser acceptance at desktop/tablet/mobile sizes, secret scans, GET-only route scans, and temporary SQLite fingerprint tests.

## MC-6.4 — Server capability

### Objectives

Add the smallest typed server read façade that complements existing host telemetry rather than duplicating it.

### Data and contract

Use existing `SystemService`, host telemetry models, psutil-backed measurements, and configuration/version sources. Add only missing concepts: host identity, CPU topology, filesystem summaries, and safe capacity state.

### Safety gate

The façade must have explicit allow-lists for filesystem roots and fields. It must never expose arbitrary process command lines, environment values, or private path inventories. All provider calls must be bounded and failure-isolated.

## MC-6.5 — Docker and container detail

### Objectives

Expose project-grouped container detail, inventory, and bounded resource history using existing Docker providers and models.

### Required boundaries

- No direct SDK usage from routes or frontend.
- No start, stop, restart, remove, prune, exec, or Compose mutation.
- No raw Docker inspect payloads.
- Bounded image, volume, network, and log metadata.
- Explicit Docker-unavailable state.

### Gate

Fake-provider tests must prove that every read route calls only observation methods. Static checks must reject lifecycle method names in the read-only capability package unless they are isolated in a future action package.

## MC-6.6 — Projects

### Objectives

Add project detail and navigation from the existing project inventory, Git snapshots, health analyzers, Compose status, and telemetry.

### Required boundaries

The read façade may inspect local branch state, dirty/conflict state, known remote-tracking state, Compose status, and health findings. It must not fetch, pull, checkout, stash, reset, clean, update, or run arbitrary project scripts.

## MC-6.7 — Systemd observation

### Objectives

Create a structured, read-only systemd observation adapter and page.

### Implementation sequence

1. Define an allow-listed unit registry.
2. Define a provider protocol for structured unit state.
3. Implement a local adapter with bounded command arguments and safe parsing.
4. Add capability façade and GET routes.
5. Add UI and TUI renderers.
6. Add static mutation guards and fake-manager tests.

### Explicit non-goals

No `enable`, `disable`, `start`, `stop`, `restart`, `reload`, `reset-failed`, unit-file installation, daemon reload, or arbitrary unit name passed from the browser is allowed.

## MC-6.8 — Logs

**Status: COMPLETE.** The bounded, redacted, read-only Logs façade/API/page was implemented and pushed at `d1f692948a014197eda60616fd602e8061959316`. The preserved Gate 2.1 harness remains SHA-256 `9e12cdc01f901381ff34b16dd68c11a14cf1158e1c32bbde928bce13c6c238e7`.

### Objectives

Provide bounded, redacted, read-only logs for operational diagnosis.

### Implementation sequence

1. Define symbolic source registry.
2. Define maximum line, byte, time, and cursor limits.
3. Implement journald/file adapters behind a protocol.
4. Add redaction before mapper serialization.
5. Add safe route and UI.
6. Link logs to existing incident/event identifiers where possible.

### Gate

The log feature must fail closed on unknown source IDs, paths, units, malformed cursors, and excessive limits. Tests must prove that secret-like values, destinations, authorization material, and raw environment output never reach API or browser fixtures.

## MC-6.9 — Incident and history expansion

**Status: NEXT DESIGN/INSPECTION ONLY.** Do not begin implementation until a separate design review and scope approval are complete.

### Objectives

Improve investigation without changing MC-3 event keys, incident correlation, schemas, or notification projection.

### Candidate extensions

- Cursor-based event/incident pagination.
- Evidence and timeline detail projections.
- History comparison queries.
- Cross-links between resource, event, incident, and history views.
- Retention-aware UI explanations.

### Gate

Use the existing read-only repositories and active-WAL regression fixture. No new event or history database is permitted.

## MC-6.10 — Settings and notification posture

**Status: PLANNED.** Notifications remain disabled; this milestone must expose posture only.

### Objectives

Make effective operational posture visible while notifications remain disabled.

### Safe projection

Expose only booleans, counts, bounded numeric values, safe enum names, version, commit, and deployment posture. Never expose raw YAML, secret references, environment variable names, destination values, or provider configuration.

### Gate

Test with enabled, disabled, empty, invalid, and partially configured temporary configurations. The dashboard must fail closed when configuration cannot be safely interpreted. No notification worker is started and no provider is instantiated.

## MC-6.11 — TUI

**Status: PLANNED.** Begin only after the shared façade contracts are stable through the preceding milestones.

### Objectives

Add an SSH/TUI surface after shared capability contracts stabilize.

### Proposed interface

```text
aipm mission-control
  overview
  server
  docker
  projects
  systemd
  logs
  incidents
  history
  notifications
  settings
```

The exact command names are subject to CLI review. The TUI should use Typer for routing and Rich for rendering, matching existing repository dependencies. It should consume shared façades directly, not scrape the HTTP dashboard.

### Gate

TUI tests must run without a live Docker daemon, systemd mutation, provider network, credentials, or live SQLite. Terminal width, Unicode, truncation, error states, and secret scanning require coverage.

## MC-6.12 — future action control plane

This milestone is not part of the first MC-6 implementation. It is listed to prevent accidental coupling between read-only observation and future operations.

A future action architecture would require separate packages and permissions:

```text
Intent → Plan → Risk classification → Human approval → Action executor
       → Idempotency/lease → Audit → Verification → Rollback/result
```

Actions must not be added to existing read façades. The action executor needs distinct service accounts/permissions, explicit allow-lists, per-action timeouts, concurrency control, audit records, and tested rollback. Public access and authentication are prerequisites.

## MC-6.13 — AI Advisor Phase 2/3/4A/4B/4C fixture presentation/4D complete

Phase 2 established deterministic normalization from bounded caller-supplied observations into immutable `EvidenceBundle` values. Phase 3 established the pure `mc613-rules-v1` engine with ten deterministic evidence-linked rules. Phase 4A adds `AdvisorCompositionRequest` and `compose_advisor()` as a pure façade over those seams. Phase 4B adds only the private authenticated read-only `POST /api/advisor/evaluate` transport boundary with bounded JSON decoding, fail-closed authentication, safe 400/401/422/500 errors, typed history-envelope reconstruction, direct Phase 4A delegation, and existing `AdvisorResponse` serialization. Phase 4D adds a telemetry-owned bounded snapshot/export and a transport-neutral adapter for the approved private-VPS CPU, memory, and disk slice. The adapter preserves configured immutable `host_id`, caller-owned `request_id` and timezone-aware `evaluation_time`, source timestamps, deterministic evidence/history identity, and fail-closed invalid/unavailable/incomplete states; it maps into canonical observations and `ResourceHistoryEnvelope` values and stops at `AdvisorCompositionRequest`.

Phase 4C adds only a fixture-driven, non-live presentation on the existing `#/ai-agent` route. It uses fixed bounded responses and provides no live advisor evaluation, browser authentication, telemetry acquisition, polling, LLM/provider functionality, actions, approvals, or runtime control. Phase 4D does not provide dashboard/UI integration, live polling, advisor evaluation, LLM/provider functionality, actions, approvals, or runtime control; it does not collect observations itself, access runtime/provider state, or invoke an LLM.

## Migration and schema strategy

MC-6 must avoid a second telemetry/event system. Existing telemetry, events, incidents, notifications, and history schemas remain authoritative.

Schema changes are permitted only when a required additive read projection cannot be implemented from existing tables. Any schema extension must:

1. Be backward-compatible with telemetry and MC-3/MC-4 writers.
2. Have a versioned migration owned by the existing repository.
3. Preserve read-only dashboard startup behavior.
4. Prove active-WAL visibility and unchanged fingerprints under read-only access.
5. Keep writer runtime behavior unchanged.
6. Include rollback/backup and retention reasoning.

The first MC-6 release should avoid schema changes entirely by composing existing projections.

## Deployment sequence

### Local design and implementation

Use temporary configuration, seeded SQLite, fake providers, and local loopback only. Run focused tests, full suite, compileall, diff checks, browser acceptance, secret scans, and read-only fingerprint checks.

### Target staging

Use a new, SHA-verified operator staging procedure only after design approval. Stage against a temporary SQLite snapshot or fixture, keep the dashboard loopback-only, verify filesystem write denial, confirm telemetry/MC-3/notification invariants, and clean up automatically.

### Production deployment

Production deployment is a separate approval gate. It should create the persistent user-level dashboard unit only after a read-only preflight verifies commit, configuration, service state, executable, port, filesystem protection, and rollback readiness. It must not change public ingress or notifications.

### Public access

The current public dashboard path is an existing bridge-bound ingress, not a change to the dashboard listener: Cloudflared container → `172.20.0.1:8788` → host nginx reverse proxy → `127.0.0.1:8787` → AIPM dashboard, serving `vpanel.03092017.xyz`. The dashboard remains loopback-bound and nginx forwards only to it. Cloudflared and Docker configuration are outside this documentation-only synchronization and were not changed by MC-6.13. Separately supplied production verification reports successful health checks through this existing path; those checks are live VPS evidence, not repository implementation evidence.

This existing ingress does not authorize new AIPM public-ingress features, authentication changes, action routes, or credential access. Any future ingress modification still requires independent threat modeling and explicit approval.

## Rollback strategy

Each milestone has a repository rollback through a reviewed commit revert or deployment rollback. Runtime rollback must be narrow:

1. Stop/disable only the MC-6 component introduced by the milestone.
2. Remove only its persistent unit or configuration fragment if it was introduced.
3. Reload the user manager only when explicitly approved.
4. Restore the previous static asset or application commit.
5. Confirm telemetry, MC-3, notifications, Docker, Cloudflare, credentials, and live SQLite are unchanged.
6. Repeat dashboard read-only and port-closure checks.

No rollback may delete the live telemetry database or checkpoint WAL as part of ordinary UI failure handling.

## Implementation governance

Before each milestone, the operator must approve the exact scope. The approval should identify:

- Source and test files allowed to change.
- Whether a schema migration is allowed.
- Whether any temporary process may start.
- Whether target-VPS staging is allowed.
- Whether systemd unit creation/reload/start is allowed.
- Whether any non-loopback access is allowed.
- Whether credentials, providers, notifications, Docker, Cloudflare, or live SQLite may be touched.

The default answer for all runtime and write operations is **not authorized**.

## Definition of done for the first MC-6 release

the MC-6.13 advisor implementation is complete through the fixture-only Phase 4C presentation and Phase 4D. The browser presentation is non-live and has no live polling, LLM, provider, advisor evaluation, or action integration.

The first read-only MC-6 release is complete only when:

- Dashboard navigation covers the approved domains.
- Existing MC-5 routes and UI behavior remain compatible.
- Server, Docker, project, Systemd, Logs, Incident, History, Notification, and Settings observations have explicit contracts or are clearly marked unavailable where not implemented.
- No new write/action route is exposed.
- Web UI and TUI share backend/core contracts where both exist.
- Freshness and unavailable states are visible and semantically correct.
- Secret and provider safety scans pass.
- Active-WAL database and sidecar fingerprints remain unchanged during dashboard/TUI reads.
- Full regression, browser, TUI, and deployment staging tests pass.
- Loopback-only deployment and rollback are documented and verified.
- Public ingress, credentials, notification activation, and remediation remain outside scope.

## References

[1]: MC-6_ARCHITECTURE.md "MC-6 architecture decisions"
[2]: MC-6_UI_SPECIFICATION.md "MC-6 UI and navigation specification"
[3]: MC-6_API_GAP_ANALYSIS.md "MC-6 API gap analysis"
[4]: MISSION_CONTROL.md "Existing Mission Control implementation history"
[5]: ../README.md "AIPM CLI and architecture"
[6]: MC-2.1_TELEMETRY_PERFORMANCE.md "Telemetry scheduling and freshness"
[7]: MC-4.5_PRODUCTION_RUNBOOK.md "Notification safety and production gates"

## Classification summary

- **EXISTS:** current read-only MC-5 dashboard and supporting telemetry/event/incident/notification capabilities.
- **EXTEND:** frontend shell, server/project/Docker detail, history, incidents, settings, notification posture, tests, and deployment documentation.
- **COMPLETE:** shared scheduler, Server, Docker, Project/Application, Systemd observation, bounded Logs, and supporting read-only adapters through MC-6.8.
- **NEW/PLANNED:** dedicated TUI and later additive projections.
- **FUTURE:** writes/actions, authentication/public ingress, SSE/WebSockets, live advisor orchestration, AI Agent execution, and notification activation.
