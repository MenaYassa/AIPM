# AIPM Mission Control Status and Roadmap

**Status date:** 2026-08-24

**Repository:** [MenaYassa/AIPM](https://github.com/MenaYassa/AIPM)

**Checkpoint:** `37d8a0ecca26f82f2a5bcfee54c26bee1e89bd70` — `feat: implement MC-6.13 Phase 4A composition`

**Remote parity:** `HEAD == origin/main`; working tree clean at the time of this status update.

## Executive summary

AIPM Mission Control has evolved from the original static VPS handbook into a **read-only operations cockpit** for host performance, Docker/container state, project/application intelligence, Systemd observations, bounded logs, event and incident context, notification posture, and historical telemetry.

The completed implementation preserves the central operating rule:

> **Mission Control observes the VPS; it does not change the VPS.**

The current committed checkpoint completes **MC-6.13 Phase 4A**. Phases 2 and 3 established immutable evidence normalization and deterministic rules; Phase 4A now adds pure request validation, recursive input snapshotting, and direct normalizer-to-rule-engine composition. Phases 4B–4E have not started and remain unauthorized. Production/runtime changes remain separate operational concerns and are not implied by the advisor domain commits.

## Completed delivery ledger

| Milestone | Delivered outcome | Status |
|---|---|---:|
| MC-1.5 | Initial Mission Control dashboard foundation and read-only service views. | Complete |
| MC-2.1 | Dual-loop telemetry with fast/slow cadence separation, freshness semantics, and regression coverage. | Complete |
| MC-3 | Deterministic event processing, incident correlation, and read-only event/incident projections. | Complete |
| MC-4 / MC-4.5 | Notification decisions, outbox/audit model, bounded retries, leases, retention, metrics, and disabled-by-default safety posture. | Complete |
| MC-5 | FastAPI read-only dashboard, existing-domain façades, static cockpit, and GET-only API surface. | Complete |
| MC-5.1–MC-5.1.2 | True read-only SQLite boundary, active-WAL visibility, filesystem write denial, and user-manager-compatible service-template hardening. | Complete |
| MC-5 Gate 2.1 | Operator staging harness validated on the target VPS and preserved in Git. | Passed |
| MC-6.1 | Shared Observation/freshness contracts, bounded queries, safety scanner, frontend state, scheduler, and shell foundation. | Complete |
| MC-6.2 | Vanilla JavaScript navigation shell, hash routing, sidebar, responsive layout, and static-module routing. | Complete |
| MC-6.3 | Server and Host Intelligence page, typed server façade/API/mapper, and safe output hardening. | Complete |
| MC-6.4 | Roadmap reconciliation; no duplicate implementation because Server Intelligence already exists through MC-6.3. | Reconciled |
| MC-6.5 | Docker/container inventory, detail, bounded resources, and project-grouped Docker intelligence. | Complete |
| MC-6.6 | Project/Application Intelligence with runtime-first inventory, Git/Compose evidence, health aggregation, and safe detail views. | Complete |
| MC-6.6.1 | Corrected project association and discovery; directory-name matching cannot create an application association. | Complete |
| MC-6.6.2 | Applications, Runtime Groups, Local Projects, and Filtered Candidates taxonomy with conservative filtering. | Complete |
| MC-6.6.3 | Health evidence aggregation, operator-friendly counts, terminology, expandable secondary sections, and UX refinement. | Complete |
| MC-6.7 | Read-only Systemd observation façade/API/page with bounded provider calls and no lifecycle controls. | Complete |
| MC-6.7.1 | Reconciled seven-entry Systemd allow-list; Cloudflared removed from Systemd and remains Docker-owned. | Complete |
| MC-6.8 | Bounded, redacted, read-only Logs façade/API/page with symbolic sources, fixed adapters, HMAC cursors, redaction-before-mapping, bounded queries, and source failure isolation. | Complete |
| MC-6.13 Phase 2 | Immutable evidence normalization with mandatory evaluation time, freshness/availability semantics, deterministic canonical serialization, stable identifiers, and explicit uncertainty. | Complete and pushed at `ebe1f84` |
| MC-6.13 Phase 3 | Pure deterministic advisor rules, canonical field schema, bounded continuity envelope, exact evidence binding, traceable recommendations, and no authority/runtime integrations. | Complete and pushed at `a7ee2f1` |
| MC-6.13 Phase 4A | Pure composition of the existing normalizer and rule engine with bounded immutable request metadata; no API/UI/LLM/runtime/action path. | Complete and pushed at `37d8a0e` |

## Current capability surface

The dashboard currently provides GET-only observations for:

- Overview and service health.
- Server and host intelligence.
- Docker summary, containers, details, images, volumes, and networks.
- Project/application inventory, detail, containers, and health.
- Allow-listed Systemd unit inventory and detail.
- Bounded logs through `/api/logs`.
- Host, container, project, resource, and tunnel history.
- Events, incidents, notification posture, channels, policies, and metrics.

The frontend remains vanilla HTML/CSS/JavaScript with static modules served through the existing `/static` mount. Polling is centralized through the shared scheduler; the Logs page uses one bounded 60-second resource. No frontend framework or build pipeline was introduced. MC-6.13 Phase 2/3/4A are backend domain-only additions and add no advisor API, dashboard view, TUI view, scheduler, LLM integration, runtime adapter, or action path.

## Read-only and ownership invariants

The following invariants remain mandatory for every future milestone:

| Boundary | Required invariant |
|---|---|
| HTTP | Observation routes are GET-only. No acknowledgement, lifecycle, mutation, download, or export route is added to read façades. |
| SQLite | Dashboard repositories use `mode=ro`, `PRAGMA query_only=ON`, and the validated filesystem write-denial boundary. No initialization, migration, schema write, checkpoint, or WAL/SHM mutation is permitted. |
| Systemd | Only genuine backend-owned allow-listed units are observable. No arbitrary unit names or lifecycle commands are accepted. |
| Docker | Docker remains the authoritative owner of container observations, including Cloudflared. No start, stop, restart, remove, prune, exec, or Compose mutation is reachable from Mission Control read façades. |
| Logs | Sources are backend-owned symbolic IDs. Paths, journal expressions, unit names, container names, commands, and provider arguments are never browser-owned. Redaction occurs before mapping and serialization. |
| Notifications | `notifications.enabled` remains `false` unless a separately approved future change explicitly changes the posture. No notification provider is activated by dashboard work. |
| Network | The dashboard remains loopback-bound at `127.0.0.1:8787`. The current Cloudflared container reaches the host-side nginx listener at `172.20.0.1:8788`, which forwards only to the loopback dashboard; the public hostname is `vpanel.03092017.xyz`. |
| Runtime | No background collector, duplicate telemetry/event store, new worker, or second database is introduced. Existing telemetry, events, incidents, notifications, and history remain authoritative. |

## Remaining Mission Control milestones

The authoritative implementation sequence is defined in [`MC-6_IMPLEMENTATION_PLAN.md`](MC-6_IMPLEMENTATION_PLAN.md).

### MC-6.9 — Incident and history evidence expansion

**Next milestone; design/inspection must come first.** The scope is bounded evidence and navigation using existing MC-3, incident, history, and read-only repository contracts. Candidate work includes cursor pagination, evidence/timeline projections, bounded history comparisons, and safe cross-links among resources, events, incidents, logs, and history. No new event/history database, schema, worker, or notification path is permitted.

The first MC-6.9 deliverable should be `docs/MC-6.9_DESIGN.md`, followed by a review checkpoint. No implementation should begin automatically.

### MC-6.10 — Settings posture and notification safety

Expose only safe scalar posture: booleans, counts, bounded numerics, enum values, version/commit, and deployment posture. Raw YAML, secret references, environment-variable names, destination values, provider configuration, and credentials remain excluded. Tests must cover enabled, disabled, empty, invalid, and partially configured temporary configurations while keeping notifications disabled and providers uninstantiated.

### MC-6.11 — Shared Typer/Rich TUI

Add a local SSH/TUI surface only after shared capability contracts stabilize. The TUI must consume the same façades and domain contracts directly rather than scraping HTTP. It must render all Observation states, truncation, terminal-width behavior, and safe errors without live infrastructure in tests.

### MC-6.12 — Future action control plane

This is not part of the current read-only cockpit. Any future action architecture requires separate identity, authorization, human approval, intent/plan models, risk classification, idempotency, leases, audit records, verification, timeout/cancellation, rollback, and dedicated permissions. Actions must not be added to existing read façades.

### MC-6.13 — AI Advisor

**Phase 2, Phase 3, and Phase 4A complete and pushed.** Phase 4A provides bounded immutable request metadata, recursive caller-input snapshots, and direct composition of the existing normalizer and rule engine. It emits the existing evidence-linked `AdvisorResponse` without aggregation or authority. No advisor API, dashboard/UI view, TUI view, LLM integration, runtime adapter, scheduler, or action authority exists. Phases 4B–4E remain future and separately authorized; any future execution—if ever approved—must occur through the separately governed MC-6.12 control plane rather than browser-generated commands or arbitrary shell access. The detailed ledger is maintained in [`MC-6.13_STATUS.md`](MC-6.13_STATUS.md).

## Deployment and operational gates

Implementation completion and production deployment are separate states.

| Gate | State | Next requirement |
|---|---|---|
| Local MC-6.8 validation | Passed | Historical MC-6.8 validation record retained. |
| Local MC-6.13 Phase 2/3 validation | Passed | Phase 2: 18 focused and 444 full tests. Phase 3 final: 29 focused and 473 full tests, with the existing unrelated Starlette/httpx warning. |
| Local MC-6.13 Phase 4A validation | Passed | 26 focused and 499 full tests, with the same unrelated Starlette/httpx warning; exact scope and protected-state checks passed. |
| MC-5 Gate 2.1 | Passed | Harness preserved with the SHA below; do not rerun without separate instruction. |
| Target-VPS production readiness | Separate operational gate | Repository work does not imply deployment or runtime validation. Operator-supplied runtime evidence remains authoritative. |
| Permanent dashboard service | Separate deployment state | The dashboard remains loopback-bound at `127.0.0.1:8787`; the current host nginx bridge listener is `172.20.0.1:8788`. |
| Public ingress/Cloudflare | Existing bridge ingress | Cloudflared container → `172.20.0.1:8788` → host nginx → `127.0.0.1:8787`; no Cloudflared or Docker configuration was changed by MC-6.13. |
| Notifications | Disabled | Must remain disabled during Mission Control development and deployment. |

The separately verified production telemetry correction is landed at `0ab4b0e859fff96add058cb3eb55e0ff408b1a83` (`fix: guard telemetry retention against lock churn and FK-poisoned batches`). This is an external production-state result, not a Phase 4A test result: the previous retention lock-churn spin fell to approximately 0% attributable CPU, five samples were observed over five minutes at the configured cadence, and no new `database is locked` entries appeared after deployment. Host-level orphan-process cleanup and configuration-twin deletion are live VPS state and are not represented as repository commits.

The preserved Gate 2.1 operator harness is:

```text
ops/staging/mc5-gate2.1-staging-v2.sh
SHA-256: 9e12cdc01f901381ff34b16dd68c11a14cf1158e1c32bbde928bce13c6c238e7
```

Any future staging must use a temporary snapshot or fixture, remain loopback-only, verify the filesystem write-denial boundary, preserve active-WAL visibility and database/WAL/SHM immutability, confirm telemetry/MC-3 state is unchanged, verify notifications remain disabled, and clean up all temporary resources automatically.

## Broader AIPM product roadmap

The separate [`PRODUCTION_ROADMAP.md`](../PRODUCTION_ROADMAP.md) concerns the broader AIPM management product, especially safe `aipm update` transactions. Its remaining work includes:

1. Typed update planning, risk classification, and fully side-effect-free dry-run behavior.
2. Explicit approval semantics and auditable `--yes` execution.
3. Critical Git change, remote divergence, conflict, detached-head, and stash transaction safety.
4. Separate planner, executor, verifier, restore-point, and rollback-manager services.
5. Structured redacted audit records for planned and executed updates.
6. Disposable Git-remote and Docker/Compose integration fixtures with CI.
7. Release hygiene including contribution, changelog, upgrade, and security documentation.
8. A separately approved, read-only inspection of the real VPS before any production update operation.

Mission Control does not replace this update roadmap. The dashboard remains a read-only observer, while future AIPM management actions require their own control plane and approvals.

## Required next sequence

The recommended order is:

1. Keep MC-6.13 Phases 4B–4E explicitly unauthorized and not started.
2. Approve and perform **MC-6.9 design/inspection only** as a separate roadmap milestone if selected.
3. Review any future composition work against the MC-6.13 pure-domain and MC-6.12A/B boundaries.
4. Independently perform any approved target-VPS production readiness or runtime validation; repository commits do not imply deployment.
5. Keep notifications, autonomous actions, advisor API/UI integration, LLM providers, and new authority paths outside the current scope unless separately approved.

## Verification baseline

The MC-6.8 checkpoint was validated with:

```text
Focused MC-6.8 tests: 11 passed
Relevant MC-5 through MC-6.7.1 regressions: 90 passed
Full pytest suite: 206 passed
Python compilation: PASS
JavaScript syntax checks: PASS
git diff --check: PASS
Mutation/lifecycle scan: PASS
Subprocess/shell safety scan: PASS
Output/secret safety scan: PASS
Frontend action/streaming scan: PASS
Production/runtime scope scan: PASS
```

The only reported test warning was the existing Starlette/httpx deprecation warning.

## Governance rule

Before each milestone, approval must identify the exact source and test files allowed to change, whether schema migration is allowed, whether any temporary process may start, whether target-VPS staging is allowed, whether Systemd operations are allowed, whether non-loopback access is allowed, and whether credentials, providers, notifications, Docker, Cloudflare, or live SQLite may be touched.

> **The default answer for runtime and write operations is not authorized.**

## References

- [`README.md`](../README.md)
- [`PROJECT_STATUS.md`](../PROJECT_STATUS.md)
- [`PRODUCTION_ROADMAP.md`](../PRODUCTION_ROADMAP.md)
- [`MC-6_IMPLEMENTATION_PLAN.md`](MC-6_IMPLEMENTATION_PLAN.md)
- [`MC-6_ARCHITECTURE.md`](MC-6_ARCHITECTURE.md)
- [`MC-6_UI_SPECIFICATION.md`](MC-6_UI_SPECIFICATION.md)
- [`MISSION_CONTROL.md`](MISSION_CONTROL.md)
- [`MC-6.8_DESIGN.md`](MC-6.8_DESIGN.md)
- [`ops/staging/mc5-gate2.1-staging-v2.sh`](../ops/staging/mc5-gate2.1-staging-v2.sh)

## Status markers

```text
MC6.8=COMPLETE
MC6.9=NEXT_DESIGN_ONLY
MC6.10=PLANNED
MC6.11=PLANNED
MC6.12=FUTURE
MC6.13=COMPLETE_THROUGH_PHASE4A
MC6.13_PHASE2=COMPLETE
MC6.13_PHASE3=COMPLETE
MC6.13_PHASE4A=COMPLETE
MC6.13_PHASE4B_TO_4E=NOT_STARTED
PRODUCTION_DEPLOYMENT=SEPARATE_APPROVAL_REQUIRED
PUBLIC_INGRESS=EXISTING_BRIDGE_INGRESS
NOTIFICATIONS=DISABLED
```

---

*This document is the current status ledger. Historical milestone design and completion documents remain preserved as audit records.*

[1]: https://github.com/MenaYassa/AIPM "AIPM repository"
