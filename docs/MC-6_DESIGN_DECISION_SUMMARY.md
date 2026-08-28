# MC-6 Design Decision Summary

> **Current-state notice — 2026-08-28:** This document is retained as part of the AIPM documentation record. Its historical design or milestone narrative remains valid as historical context, but current completion, publication, deployment, and live-observation claims are superseded by [`docs/CURRENT_STATUS.md`](CURRENT_STATUS.md) and [`docs/LIVE_VPANEL_READONLY_FINDINGS.md`](LIVE_VPANEL_READONLY_FINDINGS.md). The current tracked repository is synchronized at `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`; MC-6.12 operational execution remains blocked, and the incident-reopen workstream remains preserved separately in `stash@{0}`.


## Decision

Proceed with MC-6 as an incremental, read-only expansion of the existing AIPM Mission Control system. Retain FastAPI, Typer/Rich, the current capability/service/provider/repository layers, typed domain models, existing SQLite repositories, and the current vanilla static frontend as the initial implementation stack.

Do not introduce a second telemetry system, event store, notification database, frontend framework, background worker, public ingress, credential path, or action/control plane in the first MC-6 implementation.

## Selected architecture

| Decision | Choice |
|---|---|
| Backend | Existing FastAPI adapter plus shared capability façades and typed domain contracts. |
| CLI/TUI | Existing Typer/Rich stack; future TUI consumes the same façades directly. |
| Frontend | Incrementally extract and organize the current vanilla `index.html`; defer React/Vite migration until contracts and information architecture stabilize. |
| Data | Existing telemetry, MC-3, incident, notification, and history repositories; dashboard/TUI access remains true read-only. |
| Realtime | Bounded polling first; SSE is a later one-way observation enhancement; WebSockets are FUTURE. |
| Deployment | Dashboard remains loopback-bound at `127.0.0.1:8787`; the current public path is Cloudflared container → `172.20.0.1:8788` → host nginx reverse proxy → loopback dashboard. |
| Authentication | Cloudflare Access is the selected edge authentication boundary for the documented public ingress; AIPM relies on private edge protection and does not implement JWT verification, identity middleware, session storage, or proxy-header trust. Stronger identity-aware application behavior remains a separate future gate. |
| Writes/actions | FUTURE only; require a distinct approval, authorization, audit, idempotency, verification, and rollback control plane. |
| AI Agent | MC-6.13 Phases 2, 3, 4A, 4B, fixture-only 4C presentation, and 4D are landed; Phase 4B is a private authenticated read-only transport boundary over Phase 4A, Phase 4C is a bounded non-live presentation on the existing `#/ai-agent` route, and Phase 4D is a private-VPS telemetry-owned bounded export plus transport-neutral adapter ending at `AdvisorCompositionRequest` for CPU, memory, and disk. Phase 4B.1 records the selected Cloudflare Access edge-only boundary; live 4C orchestration, stronger application identity behavior, 4E, live polling, LLM/provider use, and execution remain future and separately gated. |

## Feature classification

- **EXISTS:** Dashboard overview, host telemetry, Docker/container overview, project inventory, history, MC-3 events, Incident Room, notification safety/audit, service freshness, loopback deployment, and current polling.
- **EXTEND:** Navigation, Server detail, Docker/project detail, history comparisons, incident evidence, notification posture, settings posture, shared client scheduler, and responsive/accessibility structure.
- **NEW:** Allow-listed Systemd observation, bounded redacted Logs, dedicated detail façades, and the shared TUI presentation.
- **FUTURE:** Any remediation or write operation, stronger application identity behavior or public-ingress changes beyond the selected Cloudflare Access edge boundary, notification activation, SSE/WebSockets, settings mutation, and AI Agent execution.

## Safety invariants

MC-6 must preserve `read_only=True`, SQLite URI `mode=ro`, `PRAGMA query_only=ON`, active-WAL visibility, filesystem write denial, unchanged database/WAL/SHM fingerprints, loopback-only binding, `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadOnlyPaths=...`, `RestrictSUIDSGID=true`, and the absence of `CapabilityBoundingSet=`.

No API or UI response may expose credentials, secret references, destination values, tokens, authorization material, raw provider payloads, tracebacks, or unnecessary private paths. No first-release route may acknowledge incidents, activate notifications, start or stop services, mutate Docker, update Git projects, execute shell commands, or change Cloudflare.

## Current implementation and recommended next step

MC-6.13 Phases 2, 3, 4A, 4B, fixture-only 4C presentation, and 4D are implemented. The current repository checkpoint is `e8f0b12d7473e3c021c536e738c8b3a414d116ad` (`feat: add fixture-only advisor presentation`). Phase 4D uses a telemetry-owned bounded export and transport-neutral observation adapter for the approved private-VPS CPU, memory, and disk slice; it is typed, deterministic, fail-closed, and ends at `AdvisorCompositionRequest`. Phase 4C uses fixed bounded response fixtures on the existing `#/ai-agent` route and does not evaluate live advice or collect current VPS state. Cloudflare Access is the selected edge authentication boundary for the documented public ingress; AIPM relies on private edge protection and does not verify JWTs or proxy identity headers.

Phase 4B.1 records the Cloudflare Access edge-only decision; live Phase 4C orchestration and stronger identity-aware application behavior remain separately authorized, while Phase 4E has not started. Phase 4B is private, authenticated, read-only, and non-authoritative, and Phase 4D does not provide live dashboard operation, live polling, advisor evaluation, LLM/provider functionality, or actions.

Production/runtime deployment remains separate from repository completion. The dashboard remains loopback-bound at `127.0.0.1:8787`; the current public path is Cloudflared container → `172.20.0.1:8788` → host nginx reverse proxy → `127.0.0.1:8787`. Notifications remain disabled, the SQLite/WAL/SHM read-only boundary remains mandatory, Cloudflared remains Docker-owned, and no-action API boundaries remain in force.

## Design status

```text
MC6_DESIGN_STATUS=COMPLETE
IMPLEMENTATION_STATUS=COMPLETE_THROUGH_MC6_13_PHASE4C_FIXTURE_AND_4D
NEXT_MILESTONE=MC6_9_DESIGN_ONLY
PRODUCTION_CHANGES=NONE
WRITE_ACTIONS_AUTHORIZED=NO
PUBLIC_INGRESS_AUTHORIZED=CLOUDFLARE_ACCESS_EDGE_ONLY
NOTIFICATIONS_ACTIVATED=NO
```

## References

[1]: MC-6_ARCHITECTURE.md "MC-6 architecture"
[2]: MC-6_UI_SPECIFICATION.md "MC-6 UI specification"
[3]: MC-6_API_GAP_ANALYSIS.md "MC-6 API gap analysis"
[4]: MC-6_IMPLEMENTATION_PLAN.md "MC-6 implementation plan"
[5]: MISSION_CONTROL.md "Existing Mission Control architecture"

## Current-state reconciliation — 2026-08-28

The canonical current-status record is [`docs/CURRENT_STATUS.md`](CURRENT_STATUS.md). The repository checkpoint is `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`, and local `HEAD`, `origin/main`, and remote `main` are equal with ahead/behind `0/0`, a clean worktree, and no staged files. The preservation stash `stash@{0}` remains intentionally untouched and contains the separate incident-reopen workstream plus `docs/MC-6.9_DESIGN.md`; those items are not part of published current main.

The read-only Mission Control cockpit is substantially landed and live. Fresh web inspection confirmed the dashboard, server, Docker, projects, bounded logs, incidents, history, settings posture, and read-only advisor surfaces. The advisor returned fresh aligned evidence with 18/18 coverage and six points spanning 300 seconds at 60-second cadence for CPU, memory, and disk. Live observations also show bounded stale/unavailable states, including stale MC-3 freshness, stale container resource observations, unavailable Systemd entries, and disabled/unavailable notification audit data. HTTP evidence does not establish the deployed Git commit, systemd unit contents, database ownership, producer convergence, or Cloudflare configuration; the live Settings surface reports `commit=Unknown`, `public_ingress=not_observed`, and `permanent_service=not_observed`.

MC-6.12 is foundation-only, not an operational action plane. No executor, action route/UI, durable operational state, leases/fencing, production target, service account, production authorization, autonomous remediation, LLM/provider execution, or notification delivery is enabled. Database merge/delete/repair/migration/rekey operations remain unauthorized.
