# AIPM Mission Control MC-6 Architecture

## Status and scope

This document is an architecture design for MC-6. It is intentionally implementation-free: it defines boundaries, contracts, information ownership, security rules, and delivery sequencing without changing application code, production configuration, systemd state, Docker, Cloudflare, credentials, providers, or live SQLite.

MC-6 extends the validated MC-5 Mission Control dashboard into an operations cockpit for an AI-operated VPS. The first MC-6 implementation remains **read-only**. Any remediation, notification activation, shell execution, update, restart, configuration mutation, or AI-directed action is classified as **FUTURE** and requires a separate approval and authorization design.

The architecture preserves the current MC-1 through MC-5 contracts:

> The dashboard observes the current VPS state; it does not repair, update, or mutate it.

The current repository baseline is FastAPI plus a Typer CLI, typed dataclass domain models, capability façades, services, providers, mappers, SQLite repositories, a single-file vanilla frontend, and separate telemetry, MC-3 event, and MC-4/4.5 notification processes. The design therefore extends existing boundaries instead of introducing a second application core, a second database layer, a second telemetry implementation, or a replacement frontend by default.

## Architectural principles

| Principle | MC-6 decision |
|---|---|
| Preserve validated safety | The MC-5.1.x `read_only=True` repository mode, SQLite `mode=ro`, `PRAGMA query_only=ON`, filesystem write-denial requirement, and loopback service boundary remain mandatory. |
| One backend contract | Web UI and future TUI consume shared capability façades, domain models, response mappers, and service contracts. They do not read SQLite or invoke Docker/systemd directly. |
| Read-only first | MC-6.1 through the initial cockpit release expose observation and history only. Write/action features are FUTURE. |
| Reuse before extension | Existing overview, history, event, incident, notification, service-health, telemetry, project, Docker, Git, and tunnel services are reused before new services are created. |
| Stable API evolution | Existing `/api/*` routes remain backward-compatible. New information is added through additive endpoints or versioned representations, not incompatible rewrites. |
| Bounded resource use | Every query has bounded limits, every log read has bounded bytes/lines, and every refresh has a fixed budget. No per-request unbounded process scans or Docker log streams are allowed. |
| Fail closed | Missing permissions, unavailable providers, invalid timestamps, unsafe paths, unsupported units, or ambiguous runtime state produce explicit unavailable/unknown states rather than guessed healthy values. |
| No blind frontend replacement | The existing `index.html` is treated as the first validated UX and migrated incrementally through stable sections and components. |
| Explicit future control plane | Any future action must pass through a separate authorization, approval, audit, idempotency, and rollback boundary. It must not be smuggled into read APIs. |

## Existing system and classification

The following classification is the design baseline for MC-6. **EXISTS** means the repository already provides a usable contract. **EXTEND** means the existing contract should gain additive fields, filters, or façade methods. **NEW** means a distinct capability/API is needed. **FUTURE** means deliberately outside the first read-only implementation.

| Product area | Classification | Existing evidence or design treatment |
|---|---|---|
| Dashboard shell and overview | EXISTS | `FastAPI`, `DashboardApi`, `DashboardTelemetryService`, `DashboardResponseMapper`, and the current static frontend already provide the cockpit shell and `/api/overview`. |
| Host CPU, memory, disk, load, uptime, network | EXISTS / EXTEND | Existing `HostSnapshot` and overview mapper are retained. Server identity, filesystem detail, process summaries, and capacity thresholds are EXTEND or NEW contracts. |
| Docker containers and resources | EXISTS / EXTEND | Existing Docker telemetry and `ContainerSnapshot` are retained. Dedicated project grouping, detail views, image/volume/network inventory, and bounded log reads are additive capabilities. |
| Project inventory | EXISTS / EXTEND | Existing `ProjectService`, Git provider path, project telemetry, and overview projection are retained. Project detail and dependency/runtime summaries are additive. |
| Resource history | EXISTS / EXTEND | Existing history façade and read-only SQLite repository remain the source of truth. Chart aggregation and comparison windows are additive query features. |
| MC-3 events and incidents | EXISTS / EXTEND | Existing read-only event/incident façades and SQLite repositories remain unchanged at the core. Search, timeline expansion, evidence views, and stable pagination are additive. |
| Notification safety and audit | EXISTS / EXTEND | Existing notification, channel, policy, metrics, and audit GET routes remain read-only and secret-safe. Effective configuration summary and delivery analytics may be extended without returning destinations or secret references. |
| Systemd inventory | NEW | A bounded, read-only systemd observation service and API are needed. It must never enable, start, stop, restart, reload, or mutate units. |
| Logs | NEW | A bounded, read-only log query service is needed. The first version should support approved local sources only, with line/byte/time limits and redaction. |
| Server detail | EXTEND / NEW | Existing host snapshot covers headline metrics. A dedicated server capability is needed for static identity, filesystems, load detail, and safe capacity summaries. |
| Settings view | EXTEND | Notification channels/policies exist. A sanitized effective-settings projection can add telemetry, events, retention, and dashboard posture without exposing paths that are unnecessary or any secret material. |
| Web UI navigation | EXTEND | The existing single page is retained and progressively organized into sections/routes without an immediate framework rewrite. |
| SSH/TUI | NEW | A Typer/Rich read-only `aipm mission-control` or `aipm dashboard tui` surface should consume the same façades and mappers. |
| Remediation and operations | FUTURE | Start/stop/restart, Compose changes, Git updates, backups/restores, systemd mutation, shell, Docker exec, and AI-directed actions are not part of the first MC-6 implementation. |
| Authentication and authorization | EDGE-PROTECTED / APPLICATION IDENTITY FUTURE | Cloudflare Access is the selected edge authentication boundary for the documented public ingress. AIPM relies on private edge protection and does not implement JWT verification, identity middleware, session storage, or proxy-header trust; stronger identity-aware behavior remains separately gated. |
| AI Agent | FIXTURE-ONLY PRESENTATION / FUTURE CONTROL | The existing `#/ai-agent` route presents bounded non-live response fixtures; agent planning, tool use, approvals, action execution, and audit still require a separate control-plane design. |

## Target logical architecture

```text
                         Browser / SSH-TUI
                              │
                 ┌────────────┴────────────┐
                 │                         │
          FastAPI HTTP adapter       Typer/Rich adapter
                 │                         │
                 └────────────┬────────────┘
                              │
                Shared Mission Control façades
          ┌─────────┬─────────┼──────────┬──────────┐
          │         │         │          │          │
       Server    Docker    Projects   Systemd     Logs
          │         │         │          │          │
          └─────────┴─────────┼──────────┴──────────┘
                              │
                    Shared domain contracts
                models + safe mappers + error states
                              │
        ┌───────────────┬─────┴──────┬────────────────┐
        │               │            │                │
 Existing providers  Read-only   Read-only       Local observation
 SystemService       telemetry   event/incident  adapters with bounds
 Docker/Git/tunnel   history DB   notification DB systemd/journal
        │               │            │                │
        └───────────────┴────────────┴────────────────┘
                              │
                        No write control plane
```

The HTTP and TUI adapters are presentation boundaries only. They may select filters, call capability façades, render the same typed results, and translate safe errors. They may not instantiate raw Docker SDK clients, call `systemctl`, open SQLite directly, read notification environment variables, or construct provider-specific payloads.

## Backend and domain boundaries

### Application composition

`Application.create()` remains the composition root for shared configuration, logging, system services, and Docker services. MC-6 should introduce a `MissionControlContext` or equivalent façade composition object only if the current constructor graph becomes too large; it must remain an application-level composition convenience, not a second service locator or database implementation.

A future context should expose capability interfaces such as `server`, `docker`, `projects`, `systemd`, `logs`, `history`, `events`, `incidents`, `notifications`, and `settings`. Each capability owns orchestration and failure isolation, while repositories and providers remain behind existing interfaces.

### Data ownership

| Data | Authoritative owner | MC-6 access rule |
|---|---|---|
| Current host state | Existing system/telemetry services | Read-only observation through a service façade. |
| Current Docker state | Existing Docker provider and telemetry services | Read-only provider calls; no lifecycle methods in MC-6 read façades. |
| Project/Git state | Existing project service and Git provider | Read-only discovery and status; no fetch/pull/checkout. |
| Tunnel visibility | Existing tunnel telemetry | Local agent/container/systemd observation only; no Cloudflare account API. |
| Historical telemetry | Existing telemetry SQLite repository | `read_only=True`, URI `mode=ro`, query-only connection, fail-closed filesystem boundary. |
| Events and incidents | Existing MC-3 repositories/services | Read-only repositories for dashboard/TUI. No acknowledgement route in MC-6 first implementation. |
| Notification audit/metrics | Existing MC-4/4.5 repository/services | Read-only repository and safe configuration projection. Notifications stay disabled. |
| Systemd state | New read-only observation adapter | Bounded `systemctl show/list-units` or equivalent read-only query; no mutation verbs. |
| Logs | New bounded read-only log adapter | Local journal or approved file source, strict allow-list, redaction, maximum bytes/lines. |
| Settings | Existing `ConfigManager` plus sanitizer | Never return secret values, environment values, destination values, or raw configuration wholesale. |

## Read-only security architecture

The read-only guarantee has multiple independent layers and MC-6 must preserve all of them:

1. **Capability-layer restriction.** MC-6 read façades expose GET-equivalent observations only. They do not import or call mutating capability methods.
2. **Repository-layer restriction.** Dashboard/TUI repositories explicitly use `read_only=True`; the connection uses SQLite URI `mode=ro` and `PRAGMA query_only=ON`; schema initialization, migration, directory creation, WAL changes, commits, and write SQL are excluded from read-only construction.
3. **Filesystem-layer restriction.** The target service must fail closed when the database directory and database sidecars are not protected against writes. `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadOnlyPaths=...`, and the validated file-mode boundary remain required.
4. **Process/service-layer restriction.** The dashboard remains loopback-only and uses `NoNewPrivileges=true`, `PrivateTmp=true`, and `RestrictSUIDSGID=true`. The unsupported `CapabilityBoundingSet=` directive remains absent.
5. **Response-layer restriction.** Mappers return safe structured states. Secrets, tokens, authorization material, destination values, environment variable values, raw command output containing credentials, and unnecessary private paths are redacted or omitted.
6. **Operational-layer restriction.** No new public ingress, Cloudflare mutation, notification activation, worker startup, Docker mutation, systemd mutation, or live database backup/checkpoint operation is part of MC-6 design work. The existing public path is protected by the selected Cloudflare Access edge boundary and remains infrastructure-owned.

## Real-time and history strategy

The initial MC-6 strategy remains bounded polling. The current frontend already polls overview and services at 15 seconds, events at 15 seconds, incidents and notifications at 30 seconds, and history at 60 seconds. MC-6 should first centralize this schedule in a small client-side data scheduler so timers are visible, deduplicated, paused when the page is hidden where safe, and never accumulated by navigation.

SSE is the preferred future enhancement for event and incident updates after the read-only HTTP contract is stable. SSE is simpler than WebSockets for one-way server-to-browser observations, works naturally with loopback and reverse proxies, and does not create an action channel. It should be introduced only after connection limits, backpressure, replay cursors, authentication, and disconnect behavior are specified. WebSockets are **FUTURE** and should be reserved for a later authenticated interactive control plane if one is ever approved.

Historical charts should continue to query the existing telemetry history API. MC-6 may add server-side aggregation, downsampling, comparison windows, and explicit freshness metadata, but it must not create a second time-series database or store opaque dashboard JSON blobs.

## Web UI and TUI relationship

The Web UI is the primary current presentation. The future TUI should use the same capability façade and domain models, with a Rich renderer for tables, panels, trend summaries, and incident timelines. It must not scrape HTTP responses as its primary architecture and must not duplicate SQLite, Docker, Git, or systemd access.

The HTTP mapper and TUI renderer may differ in presentation shape, but both must consume the same semantic states: `fresh`, `stale`, `unavailable`, `never_sampled`, `unknown`, and explicit error codes. This keeps the operational meaning consistent across browser and SSH sessions.

## Authentication and authorization boundary

The AIPM dashboard remains loopback-bound on `127.0.0.1:8787`. The current public path is an existing bridge-bound ingress: Cloudflared container → `172.20.0.1:8788` → host nginx reverse proxy → `127.0.0.1:8787`. The public hostname is `vpanel.03092017.xyz`; nginx forwards only to the loopback dashboard.

Cloudflare Access is the selected and confirmed authentication boundary for this existing public ingress. AIPM relies on the private edge protection and does not verify Cloudflare JWTs or identity headers, provide identity middleware, manage browser sessions, or trust proxy headers. The edge boundary does not grant AIPM application authority, credential access, or action routes. Stronger identity-aware application behavior remains a separate future decision and would require explicit identity, authorization, threat modeling, rate limits, session/CSRF controls where applicable, and secret-safe error handling. No browser-held provider credential is acceptable.

## Deployment topology

The first persistent topology is one user-level read-only dashboard process alongside the existing telemetry sampler and MC-3 event processor:

```text
user systemd manager
├── aipm-telemetry.service   (existing writer)
├── aipm-events.service      (existing event processor)
├── aipm-dashboard.service   (read-only observer)
└── aipm-notifications.service (not enabled while notifications.enabled=False)
```

The dashboard reads the live telemetry database through the validated read-only boundary. It does not run telemetry sampling, event processing, notification delivery, Docker lifecycle operations, Git fetches, or background remediation. The service remains loopback-only. The existing host nginx bridge is the ingress layer and forwards `172.20.0.1:8788` only to `127.0.0.1:8787`; it is not an AIPM application authority or a second database/worker.

## Testing architecture

MC-6 testing is layered:

| Layer | Required coverage |
|---|---|
| Domain/model | Freshness, error, pagination, redaction, grouping, sorting, and stable enums. |
| Capability/service | Provider failure isolation, bounded calls, no mutating method invocation, read-only repository construction, and safe unavailable states. |
| Repository | Active-WAL reads, fingerprint stability, write rejection, missing path fail-closed behavior, and no schema/migration side effects. |
| API | Route status, query bounds, stable JSON shape, GET-only first contract, secret scan, action-route absence, and error handling. |
| TUI | Shared façade usage, rendering of all state classes, truncation, terminal-width behavior, and no direct provider/database imports. |
| UI | Section rendering, responsive layout, scheduler behavior, empty/error states, accessibility labels, and no action controls. |
| Integration | Temporary seeded SQLite, fake providers, isolated systemd/journal adapters, and no external network calls. |
| Deployment | Loopback binding, user-systemd syntax, filesystem protections, port collision, rollback, and unchanged telemetry/MC-3 processes. |

## Migration strategy from the current frontend

The current `src/aipm/dashboard/static/index.html` is not discarded. MC-6 should proceed in three controlled steps:

1. Extract repeated inline JavaScript helpers into a small static module only after snapshot tests cover the current behavior.
2. Introduce a navigation shell and section registry while retaining the current overview sections and endpoint contracts.
3. Add new read-only pages incrementally, with each page backed by a capability/API contract and a dedicated test slice.

A React/Vite migration is not the first implementation step. It may be reconsidered after the information architecture, API gaps, and UI component boundaries have stabilized. A migration must preserve URL contracts, polling semantics, accessibility, loopback deployment simplicity, and the no-secret/no-action safety posture.

## Architectural decisions

1. **Backend:** retain FastAPI, Typer, existing capability/service/provider/repository layers, and typed dataclasses.
2. **Frontend:** evolve the current vanilla static frontend incrementally; do not replace it blindly.
3. **Realtime:** bounded polling first; SSE later for one-way event updates; WebSockets only FUTURE.
4. **Storage:** reuse the existing SQLite database and repositories; no second telemetry or event store.
5. **Systemd:** introduce a read-only observation adapter as NEW, but keep systemd mutation entirely FUTURE.
6. **Logs:** introduce bounded, redacted, read-only log access as NEW; no arbitrary shell execution.
7. **Access:** the dashboard remains loopback-bound; the existing public path uses a bridge-bound nginx listener at `172.20.0.1:8788` forwarding to `127.0.0.1:8787`, with Cloudflare Access as the selected edge authentication boundary. AIPM relies on that private edge protection and does not implement JWT verification, identity middleware, session storage, or proxy-header trust. Any stronger identity-aware application behavior remains separately gated.
8. **Actions:** all writes and remediation are FUTURE and require a dedicated approval/control plane.
9. **TUI:** build after shared façade contracts stabilize; share backend/core contracts, not HTTP scraping.
10. **Delivery:** implement in small, independently testable vertical slices with explicit rollback at every stage.

## References

[1]: ../README.md "AIPM README and architecture overview"
[2]: MISSION_CONTROL.md "Existing Mission Control architecture and safety boundaries"
[3]: MC-3_ARCHITECTURE.md "MC-3 event and incident architecture"
[4]: MC-4_ARCHITECTURE.md "MC-4 notification architecture"
[5]: MC-4.5_PRODUCTION_RUNBOOK.md "MC-4.5 production hardening runbook"
[6]: ../src/aipm/dashboard/server.py "FastAPI dashboard adapter"
[7]: ../src/aipm/capabilities/dashboard/api.py "Dashboard capability façade"
[8]: ../src/aipm/models/telemetry.py "Typed telemetry and freshness contracts"
[9]: ../ops/systemd/aipm-dashboard.service "Validated production dashboard service template"

## Current implementation reconciliation

**Updated:** 2026-08-25

The MC-6 architecture described above is implemented through **MC-6.8** plus MC-6.13 Phase 2/3/4A/4B, fixture-only Phase 4C presentation, and 4D. The completed slices include immutable evidence normalization, deterministic advisor rules, pure composition, the private authenticated read-only advisor API, the fixture-only non-live advisor presentation on the existing `#/ai-agent` route, and a private-VPS telemetry-owned bounded snapshot/export with a transport-neutral advisor observation adapter. The Phase 4B route uses bounded transport decoding, fail-closed authentication, direct Phase 4A delegation, and existing response serialization. Phase 4D uses the telemetry owner boundary for SQLite/WAL/SHM interaction, promotes only the approved CPU, memory, and disk slice, and stops at `AdvisorCompositionRequest`. The current pushed checkpoint is `e8f0b12d7473e3c021c536e738c8b3a414d116ad`.

MC-6.13 Phase 2/3/4A uses an isolated pure advisor domain boundary over immutable evidence and caller-supplied context; Phase 4B adds only a private authenticated transport adapter over that boundary; Phase 4C adds only a fixture-driven, non-live presentation on the existing dashboard route; and Phase 4D adds only the bounded telemetry export and canonical observation adapter. The Phase 4C surface does not access SQLite, database paths, WAL/SHM, filesystem, network, current clocks, randomness, providers, actions, approvals, or runtime control, and does not invoke Phase 4A or Phase 3. The Phase 4D adapter does not access those browser/runtime authorities either. No live observation poller, second telemetry/event store, provider ownership layer, advisor evaluation path, LLM integration, or action path was introduced.

Phase 4B.1 records the selected Cloudflare Access edge-only boundary. AIPM relies on private edge protection and does not implement JWT verification, identity middleware, session storage, or proxy-header trust. The fixture-only Phase 4C presentation is landed; live Phase 4C orchestration, stronger application identity behavior, and Phase 4E remain future, unauthorized, and not started. Phase 4B remains private, authenticated, read-only, and transport-only; Phase 4D remains private-VPS, bounded, typed, and non-runtime.

The deployment architecture remains separate from advisor implementation. The dashboard is loopback-bound at `127.0.0.1:8787`; the existing public path is Cloudflared container → `172.20.0.1:8788` → host nginx reverse proxy → `127.0.0.1:8787`, serving `vpanel.03092017.xyz`. Notifications remain disabled, Cloudflared remains Docker-owned, and any future runtime or ingress change requires separate approval. Phase 4C is only a non-live fixture presentation; Phase 4D does not provide live dashboard operation, live polling, API/UI integration, LLM/provider functionality, actions, or approvals. The preserved Gate 2.1 harness SHA-256 is `9e12cdc01f901381ff34b16dd68c11a14cf1158e1c32bbde928bce13c6c238e7`.

## Classification

- **EXISTS:** current MC-1 through MC-5 read-only dashboard, telemetry, history, events, incidents, notification audit, service health, and loopback deployment contracts.
- **EXTEND:** navigation, grouping, chart queries, safe settings projection, filters, pagination, and detail views that can build on existing sources.
- **NEW:** bounded systemd observation, bounded log observation, dedicated server/project/container detail façades, and the first shared TUI adapter.
- **FUTURE:** remediation, actions, public ingress, authentication/authorization implementation, SSE, WebSockets, and AI Agent control-plane integration.
