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
| Authentication | Local/SSH access first; authenticated public access is a separate future gate. |
| Writes/actions | FUTURE only; require a distinct approval, authorization, audit, idempotency, verification, and rollback control plane. |
| AI Agent | MC-6.13 Phase 2/3 advisor domain is landed; Phase 4 composition and any execution remain future and separately gated. |

## Feature classification

- **EXISTS:** Dashboard overview, host telemetry, Docker/container overview, project inventory, history, MC-3 events, Incident Room, notification safety/audit, service freshness, loopback deployment, and current polling.
- **EXTEND:** Navigation, Server detail, Docker/project detail, history comparisons, incident evidence, notification posture, settings posture, shared client scheduler, and responsive/accessibility structure.
- **NEW:** Allow-listed Systemd observation, bounded redacted Logs, dedicated detail façades, and the shared TUI presentation.
- **FUTURE:** Any remediation or write operation, authentication/public ingress, notification activation, SSE/WebSockets, settings mutation, and AI Agent execution.

## Safety invariants

MC-6 must preserve `read_only=True`, SQLite URI `mode=ro`, `PRAGMA query_only=ON`, active-WAL visibility, filesystem write denial, unchanged database/WAL/SHM fingerprints, loopback-only binding, `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadOnlyPaths=...`, `RestrictSUIDSGID=true`, and the absence of `CapabilityBoundingSet=`.

No API or UI response may expose credentials, secret references, destination values, tokens, authorization material, raw provider payloads, tracebacks, or unnecessary private paths. No first-release route may acknowledge incidents, activate notifications, start or stop services, mutate Docker, update Git projects, execute shell commands, or change Cloudflare.

## Current implementation and recommended next step

MC-6.1 through MC-6.8 have been implemented, reviewed, validated, committed, and pushed. MC-6.4 was reconciled because the Server capability already existed through MC-6.3. MC-6.7.1 reconciled the Systemd registry so Cloudflared remains Docker-owned, and MC-6.8 delivered the bounded redacted Logs façade/API/page. MC-6.13 Phases 2, 3, and 4A are also implemented, reviewed, committed, and pushed.

The current checkpoint is `37d8a0ecca26f82f2a5bcfee54c26bee1e89bd70`. MC-6.13 Phase 2 provides immutable evidence normalization, Phase 3 provides the pure deterministic `mc613-rules-v1` engine with ten evidence-linked rules, and Phase 4A provides pure bounded composition over those seams. Phases 4B–4E have not started and remain unauthorized. MC-6.9 remains the next separate design/inspection milestone; MC-6.10 and MC-6.11 remain planned, and MC-6.12 remains the future action-control boundary.

Production/runtime deployment remains separate from repository completion. The dashboard remains loopback-bound at `127.0.0.1:8787`; the current public path is Cloudflared container → `172.20.0.1:8788` → host nginx reverse proxy → `127.0.0.1:8787`. Notifications remain disabled, the SQLite/WAL/SHM read-only boundary remains mandatory, Cloudflared remains Docker-owned, and no-action API boundaries remain in force.

## Design status

```text
MC6_DESIGN_STATUS=COMPLETE
IMPLEMENTATION_STATUS=COMPLETE_THROUGH_MC6_13_PHASE4A
NEXT_MILESTONE=MC6_9_DESIGN_ONLY
PRODUCTION_CHANGES=NONE
WRITE_ACTIONS_AUTHORIZED=NO
PUBLIC_INGRESS_AUTHORIZED=EXISTING_BRIDGE_ONLY
NOTIFICATIONS_ACTIVATED=NO
```

## References

[1]: MC-6_ARCHITECTURE.md "MC-6 architecture"
[2]: MC-6_UI_SPECIFICATION.md "MC-6 UI specification"
[3]: MC-6_API_GAP_ANALYSIS.md "MC-6 API gap analysis"
[4]: MC-6_IMPLEMENTATION_PLAN.md "MC-6 implementation plan"
[5]: MISSION_CONTROL.md "Existing Mission Control architecture"
