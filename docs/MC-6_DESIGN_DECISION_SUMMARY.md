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
| Deployment | Loopback-only user-level systemd dashboard alongside existing telemetry and MC-3 services. |
| Authentication | Local/SSH access first; authenticated public access is a separate future gate. |
| Writes/actions | FUTURE only; require a distinct approval, authorization, audit, idempotency, verification, and rollback control plane. |
| AI Agent | FUTURE advisor first, with execution only through the future action control plane. |

## Feature classification

- **EXISTS:** Dashboard overview, host telemetry, Docker/container overview, project inventory, history, MC-3 events, Incident Room, notification safety/audit, service freshness, loopback deployment, and current polling.
- **EXTEND:** Navigation, Server detail, Docker/project detail, history comparisons, incident evidence, notification posture, settings posture, shared client scheduler, and responsive/accessibility structure.
- **NEW:** Allow-listed Systemd observation, bounded redacted Logs, dedicated detail façades, and the shared TUI presentation.
- **FUTURE:** Any remediation or write operation, authentication/public ingress, notification activation, SSE/WebSockets, settings mutation, and AI Agent execution.

## Safety invariants

MC-6 must preserve `read_only=True`, SQLite URI `mode=ro`, `PRAGMA query_only=ON`, active-WAL visibility, filesystem write denial, unchanged database/WAL/SHM fingerprints, loopback-only binding, `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadOnlyPaths=...`, `RestrictSUIDSGID=true`, and the absence of `CapabilityBoundingSet=`.

No API or UI response may expose credentials, secret references, destination values, tokens, authorization material, raw provider payloads, tracebacks, or unnecessary private paths. No first-release route may acknowledge incidents, activate notifications, start or stop services, mutate Docker, update Git projects, execute shell commands, or change Cloudflare.

## Current implementation and recommended next step

MC-6.1 through MC-6.8 have now been implemented, reviewed, validated, committed, and pushed. MC-6.4 was reconciled because the Server capability already existed through MC-6.3. MC-6.7.1 reconciled the Systemd registry so Cloudflared remains Docker-owned, and MC-6.8 delivered the bounded redacted Logs façade/API/page.

The current checkpoint is `d1f692948a014197eda60616fd602e8061959316`. The next milestone is **MC-6.9 design/inspection only**, covering bounded incident/history evidence, comparison queries, cursor pagination, and safe cross-links through existing read-only repositories and MC-3 contracts. MC-6.9 must stop for review before implementation. MC-6.10 Settings posture and MC-6.11 TUI remain planned; MC-6.12 action control and MC-6.13 AI Agent integration remain future and separately gated.

Production deployment remains a separate approval gate. The loopback-only dashboard, notifications-disabled posture, read-only SQLite/WAL/SHM boundary, Cloudflared ownership rule, and no-action API boundary remain mandatory.

## Design status

```text
MC6_DESIGN_STATUS=COMPLETE
IMPLEMENTATION_STATUS=COMPLETE_THROUGH_MC6_8
NEXT_MILESTONE=MC6_9_DESIGN_ONLY
PRODUCTION_CHANGES=NONE
WRITE_ACTIONS_AUTHORIZED=NO
PUBLIC_INGRESS_AUTHORIZED=NO
NOTIFICATIONS_ACTIVATED=NO
```

## References

[1]: MC-6_ARCHITECTURE.md "MC-6 architecture"
[2]: MC-6_UI_SPECIFICATION.md "MC-6 UI specification"
[3]: MC-6_API_GAP_ANALYSIS.md "MC-6 API gap analysis"
[4]: MC-6_IMPLEMENTATION_PLAN.md "MC-6 implementation plan"
[5]: MISSION_CONTROL.md "Existing Mission Control architecture"
