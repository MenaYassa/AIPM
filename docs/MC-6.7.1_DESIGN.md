# MC-6.7.1 Design: Verified Infrastructure Systemd Allow-List Refinement

> **Current-state notice — 2026-08-28:** This document is retained as part of the AIPM documentation record. Its historical design or milestone narrative remains valid as historical context, but current completion, publication, deployment, and live-observation claims are superseded by [`docs/CURRENT_STATUS.md`](CURRENT_STATUS.md) and [`docs/LIVE_VPANEL_READONLY_FINDINGS.md`](LIVE_VPANEL_READONLY_FINDINGS.md). The current tracked repository is synchronized at `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`; MC-6.12 operational execution remains blocked, and the incident-reopen workstream remains preserved separately in `stash@{0}`.


**Design review status:** Complete and approved (historical design checkpoint)
**Design baseline:** `64d5bb52d017317cff7488fcc4f1859fbb680818`
**Implementation status:** Complete and pushed in the subsequent MC-6.7.1 checkpoint
**Scope:** Backend-owned Systemd registry refinement only; no source, test, runtime, deployment, or configuration changes were authorized by the original design.
**Current status note:** The seven-entry registry is implemented. Cloudflared is absent from the Systemd registry and remains Docker-owned. MC-6.8 and its Logs design/implementation are also complete; MC-6.9 remains unstarted.

## 1. Objective

MC-6.7.1 refines the MC-6.7 backend-owned Systemd allow-list so that Mission Control can represent the verified important infrastructure services on the target VPS without weakening the existing fail-closed, read-only architecture.

The refinement is intentionally narrow. The current provider, observation service, façade, API routes, mapper, frontend page, and scheduler already provide the required observation vertical slice. The proposed implementation should therefore extend the internal registry and typed opaque identifiers only, unless validation discovers a compatibility defect. It must not become a host-wide unit inventory or a control plane.

The verified service evidence supplied for this design is:

| Manager | Verified service |
|---|---|
| User manager | `aipm-dashboard.service` |
| User manager | `aipm-telemetry.service` |
| User manager | `aipm-events.service` |
| User manager | `freebuff-llm-proxy.service` |
| System manager | `fastsd-webui.service` |
| System manager | `fastsd-webserver.service` |
| System manager | `fastsd-proxy.service` |
| Docker observation | Cloudflared container; no corresponding `cloudflared.service` exists |

The earlier service list included `cloudflared.service`, but the later VPS inspection is authoritative for ownership classification: no such Systemd unit exists on the target VPS, while Cloudflared is represented as a Docker container. This design therefore treats Cloudflared as a Docker-owned component, not a Systemd-owned component. No VPS connection, live manager query, service restart, or runtime inspection is authorized by MC-6.7.1.

## 2. Current MC-6.7 allow-list

The committed MC-6.7 registry currently contains four backend-owned opaque IDs:

| Opaque ID | Display name | Manager scope | Manager unit |
|---|---|---|---|
| `aipm-dashboard` | AIPM Dashboard | `user` | `aipm-dashboard.service` |
| `aipm-telemetry` | AIPM Telemetry | `user` | `aipm-telemetry.service` |
| `aipm-events` | AIPM Events | `user` | `aipm-events.service` |
| `cloudflared` | Cloudflared Tunnel | `system` | `cloudflared.service` |

The current implementation already keeps the browser-facing ID separate from the manager unit name. Provider command construction is internal and fixed: user units use `systemctl --user show <registry-unit> ...`, while system units use `systemctl show <registry-unit> ...`. The current `cloudflared` entry is a stale registry classification that must be removed during the approved MC-6.7.1 implementation; the existing narrow telemetry fallback is a separate compatibility path and is not evidence that a Systemd unit exists.

> **Systemd-versus-Docker ownership principle:** A component must be represented under Systemd only when a real allow-listed Systemd unit exists in the corresponding manager. Docker containers must remain Docker observations unless a genuine Systemd unit independently manages them. Application importance, service naming, or container identity must never be used to infer Systemd ownership.

## 3. Proposed allow-list

The proposed MC-6.7.1 registry contains seven entries. Three existing AIPM user-manager entries retain their IDs and semantics; four verified non-Cloudflared services are represented with stable opaque IDs. The current `cloudflared` Systemd registry entry is deliberately removed because the target VPS has no corresponding Systemd unit.

| Opaque ID | Display name | Manager scope | Manager unit | Inclusion decision |
|---|---|---|---|---|
| `aipm-dashboard` | AIPM Dashboard | `user` | `aipm-dashboard.service` | Keep |
| `aipm-telemetry` | AIPM Telemetry | `user` | `aipm-telemetry.service` | Keep |
| `aipm-events` | AIPM Events | `user` | `aipm-events.service` | Keep |
| `freebuff-llm-proxy` | Freebuff LLM Proxy | `user` | `freebuff-llm-proxy.service` | Add |
| `fastsd-webui` | FastSD Web UI | `system` | `fastsd-webui.service` | Add |
| `fastsd-webserver` | FastSD Web Server | `system` | `fastsd-webserver.service` | Add |
| `fastsd-proxy` | FastSD Proxy | `system` | `fastsd-proxy.service` | Add |
The names and purposes of the FastSD entries are classified conservatively from their verified unit names: `fastsd-webui` is presumed to represent a web UI, `fastsd-webserver` an application/web server, and `fastsd-proxy` a proxy or gateway component. The repository contains no FastSD unit files or service implementation from which stronger purpose claims can be established. Their exact runtime behavior, dependency graph, network exposure, ports, and public reachability are therefore **unavailable in this milestone** and must not be inferred from the names.

All seven proposed Systemd entries should be included because the operator has identified them as important services and each proposed entry has a concrete manager scope and exact unit name. Inclusion means only that the service is eligible for bounded read-only observation. It does not imply health, availability, enablement, network safety, public exposure, or dependency correctness. Cloudflared is intentionally excluded from this list and remains represented by Docker intelligence.

## 4. Manager compatibility analysis

The existing MC-6.7 provider already supports both manager scopes safely. `SystemdUnitRegistryEntry.manager_scope` is an internal registry field. The local adapter accepts only the two internally configured manager scopes, maps both to the fixed executable `systemctl`, and constructs either:

```text
systemctl --user show <fixed-registry-unit> --no-pager --property=LoadState,ActiveState,SubState,UnitFileState
systemctl show <fixed-registry-unit> --no-pager --property=LoadState,ActiveState,SubState,UnitFileState
```

The browser supplies neither a manager scope nor a manager command. An opaque ID is resolved against the backend-owned registry before the provider is called. An unknown ID returns a safe allow-list error and does not reach the provider.

Therefore, the four new services can be added through registry-only changes in the normal implementation path:

1. Extend `SystemdUnitId` with four stable values.
2. Add four `SystemdUnitRegistryEntry` records with fixed unit names and scopes.
3. Extend registry/model tests and frontend/API inventory expectations.

No provider command construction change is expected. No service, façade, mapper, route, or frontend architecture change is expected unless tests reveal an incidental count or presentation assumption.

## 5. EXISTS / EXTEND / NEW / UNAVAILABLE

| Area | Classification | MC-6.7.1 conclusion |
|---|---|---|
| Opaque backend-owned unit IDs | EXISTS / EXTEND | Extend the enum and registry; preserve existing values. |
| User/system manager selection | EXISTS | Reuse `manager_scope`; no new manager abstraction is required. |
| Fixed `systemctl` command construction | EXISTS | Do not alter executable, argument template, or manager mapping. |
| `shell=False`, timeout, bounded output | EXISTS | Preserve unchanged. |
| Per-unit failure isolation | EXISTS | Preserve unchanged. |
| Read-only observation service | EXISTS | No service redesign; registry expansion is consumed automatically. |
| GET-only façade/routes | EXISTS | No route changes expected. |
| Systemd frontend inventory | EXISTS | Existing page will consume the larger allow-list automatically. |
| Existing cloudflared fallback | EXISTS | Preserve the narrow telemetry fallback; do not refactor or replace it; it is not a Systemd inventory entry. |
| Cloudflared Systemd ownership | RECONCILED / REMOVE | Remove the stale `cloudflared` Systemd registry entry; Docker intelligence remains the authoritative Mission Control representation unless a genuine Systemd unit is later verified. |
| Freebuff service purpose | EXTEND / PARTIALLY AVAILABLE | Unit identity is verified by operator evidence; application semantics are not established in repository code. |
| FastSD service purpose | EXTEND / PARTIALLY AVAILABLE | Names provide conservative labels only; unit files, dependencies, and behavior are unavailable. |
| FastSD network exposure | UNAVAILABLE / OUT OF SCOPE | Do not investigate or change it in MC-6.7.1; record as a separate security follow-up. |
| Live manager availability | UNAVAILABLE | No VPS access is authorized. Local fake-manager tests remain the validation mechanism. |
| Unit history, logs, dependencies, process inspection | UNAVAILABLE / OUT OF SCOPE | Defer to later milestones; do not expand this registry refinement. |

## 6. Compatibility and safety analysis

Registry-only extension is compatible with the current provider because each new entry supplies the same fields already required by the adapter: opaque ID, display label, exact unit name, and manager scope. The current user/system branch is sufficient for the four new Systemd services. Removing the stale Cloudflared registry entry is also provider-compatible: unknown `cloudflared` IDs will fail closed, while the Docker intelligence and narrow tunnel fallback remain independent.

The safety boundary remains unchanged:

- The browser receives only opaque IDs and bounded list/detail parameters.
- No arbitrary unit name, manager name, executable, property list, or argument list reaches the provider.
- The manager executable remains fixed to `systemctl`.
- The adapter continues to use fixed tuples, `shell=False`, a two-second timeout, bounded stdout, and allow-listed parsed properties.
- Only `show` observations are permitted. No start, stop, restart, reload, enable, disable, reset-failed, daemon-reload, mask, unmask, edit, or other lifecycle operation is introduced.
- Unknown IDs fail closed before provider access.
- A failure for one unit remains isolated from unrelated unit observations.
- Raw stdout/stderr, environment variables, command lines, unit-file contents, credentials, tokens, arbitrary paths, and unbounded properties remain excluded.
- Systemd configuration files, service templates, manager state, and unit enablement are not modified.

The appearance of system-manager services in the registry does not authorize any system-manager mutation. It only allows a fixed read-only observation attempt, which may safely result in unavailable/error if the dashboard process lacks permission or the manager is inaccessible.

## 7. Cloudflared ownership reconciliation

The current MC-6.7 registry contains `cloudflared` as a system-manager unit, but the verified VPS inspection shows that `cloudflared.service` does not exist. Cloudflared is represented as a Docker container. The correct classification is therefore:

| Component | Systemd intelligence | Docker intelligence | Decision |
|---|---|---|---|
| Cloudflared | No genuine allow-listed Systemd unit | Existing Docker/container observation | Remove from Systemd registry; retain under Docker intelligence |

This is an ownership correction, not a health or deployment judgment. The Systemd list route must not claim Cloudflared as an observable Systemd unit merely because it is operationally important or because a container is named Cloudflared. The existing tunnel telemetry fallback in `src/aipm/services/telemetry/tunnel.py` remains unchanged for compatibility: it can observe the container first and use its fixed, failure-isolated legacy fallback without making Cloudflared a member of the general Systemd registry.

## 8. FastSD classification and separate security follow-up

The verified FastSD services are classified as a three-part system-manager application surface:

| Service | Conservative classification | Evidence level |
|---|---|---|
| `fastsd-webui.service` | FastSD web user-interface component | Operator-provided unit name only |
| `fastsd-webserver.service` | FastSD web/application server component | Operator-provided unit name only |
| `fastsd-proxy.service` | FastSD proxy/gateway component | Operator-provided unit name only |

MC-6.7.1 must not inspect their network bindings, ports, reverse-proxy routes, public ingress, TLS, authentication, or Cloudflare relationship. Those are separate security and deployment questions requiring an explicitly approved follow-up. Systemd observation status must not be presented as proof that a FastSD endpoint is private, authenticated, or safe to expose.

## 9. Exact implementation file plan for a later approved implementation

The expected implementation should be limited to:

| File | Expected change |
|---|---|
| `src/aipm/models/systemd.py` | Remove the stale `CLOUDFLARED` ID/entry, add four opaque IDs for Freebuff and FastSD, and retain the three AIPM entries. Preserve all other enum and registry behavior. |
| `tests/test_mc67_systemd.py` | Update the exact registry set and add assertions for user/system command mapping and the four new entries. Preserve unknown-ID, failure, bounds, output-safety, and GET-only tests. |

No change is expected to:

```text
src/aipm/providers/systemd.py
src/aipm/services/systemd/observation.py
src/aipm/capabilities/dashboard/systemd_api.py
src/aipm/mappers/systemd.py
src/aipm/dashboard/server.py
src/aipm/dashboard/static/index.html
src/aipm/dashboard/static/mission-control-systemd.js
ops/systemd/*
ops/staging/*
```

If implementation tests expose a genuine defect in manager mapping or response bounds, stop and report it rather than silently expanding scope. The default plan is registry-only.

## 10. Required tests for implementation approval

A later implementation phase must run, at minimum:

| Test area | Required proof |
|---|---|
| Registry | Exactly seven entries; all IDs are stable and unique; Cloudflared is absent; new unit names and scopes are exact. |
| Manager mapping | User services use `systemctl --user`; system services use `systemctl`; no other executable or argument source is accepted. |
| Unknown IDs | Arbitrary IDs fail closed before provider access. |
| Read-only provider | New entries use `show` only; no lifecycle/control method is reachable. |
| Failure isolation | One FastSD or Freebuff failure does not invalidate unrelated unit observations. |
| Output safety | New entries do not expose raw output, paths, environment, command lines, credentials, or arbitrary properties. |
| API compatibility | Existing GET routes remain unchanged; Systemd routes remain GET-only and bounded. |
| Frontend | The existing page displays all allow-listed entries without a second scheduler or action controls. |
| Regression | MC-5 through MC-6.7 suites, compilation, JavaScript syntax, diff checks, mutation scans, and the preserved Gate 2.1 harness identity. |
| Integration boundary | Fake manager only; no VPS, live Systemd manager, database, Docker daemon, provider, credential, or network access. |

## 11. Explicit non-goals

MC-6.7.1 does not add Systemd lifecycle actions, process detection, logs, dependency graphs, history, alerts, notification activation, authentication, public ingress, network exposure analysis, service deployment, manager configuration, systemd unit installation, Docker integration, Cloudflare integration, credentials, or MC-6.8 work.

It does not reinterpret a service as healthy merely because it is allow-listed or active. It does not claim that FastSD services are network-safe. It does not change Cloudflared telemetry semantics, replace the existing fallback, or make the telemetry fallback depend on the Systemd page. It does not infer Systemd ownership from application importance or Docker container identity.

## 12. Stop condition before implementation

This design phase is complete only when the repository contains this document and no source, test, configuration, systemd, deployment, runtime, database, Docker, Cloudflare, credential, or provider files have changed.

Implementation must not begin until separately approved. If approved, it must remain registry-only unless a test demonstrates a necessary compatibility correction. After implementation and validation, stop for review before any commit, push, VPS operation, or deployment.

The next milestone after MC-6.7.1 implementation remains **MC-6.8**, but MC-6.8 must not start from this design phase.

## 13. Design-only validation markers

```text
MC6.7.1_DESIGN=COMPLETE
MC6.7.1_IMPLEMENTATION_STARTED=NO
MC6.8_STARTED=NO
SOURCE_CHANGES=0
TEST_CHANGES=0
RUNTIME_CHANGES=0
SYSTEMD_CONFIGURATION_CHANGES=0
DEPLOYMENT=NO
```
