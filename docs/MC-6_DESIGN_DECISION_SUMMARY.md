# MC-6 Design Decision Summary

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
