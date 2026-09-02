# AIPM Current Status

**Status date:** 2026-09-01 (supersedes the 2026-08-28 reconciliation; see the final section)

**Canonical repository checkpoint:** `ee308ac600f2148166efa1146a455b6bc1fe2a06` (18 commits ahead of the previously published checkpoint `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`)

**Repository parity:** `HEAD` is `3` commits ahead of `origin/main`, `0` behind; the MC-6.12 telemetry/service-runtime remediation patch is present in the working tree but **not yet committed** (see the final section).

## Purpose of this document

This is the canonical current-state reconciliation for the AIPM repository and Mission Control. Historical design and completion documents remain preserved as audit records, but their older checkpoint and “planned/future” wording must be interpreted through this document. The detailed read-only public inspection is preserved in [`LIVE_VPANEL_READONLY_FINDINGS.md`](LIVE_VPANEL_READONLY_FINDINGS.md).

## Executive status

The read-only Mission Control cockpit is substantially landed and live. The public vpanel provides functioning read-only surfaces for the dashboard, host/server intelligence, Docker, projects, Systemd observation, bounded logs, incidents, history, settings posture, and the advisor. MC-6.13 is complete through Phase 4E for its explicitly bounded read-only advisor scope.

The repository is fully committed and published at the checkpoint above. This does not mean that every historical workstream is committed: the preservation stash still contains the separate incident-reopen workstream and `docs/MC-6.9_DESIGN.md`. Those items were intentionally not merged into current main.

MC-6.12 is **not operationally complete**. Current main contains Stage 2 pure control-plane models and a Stage 3 staging-only, process-local owner authentication/session/project-plan foundation. It does not contain an executor, action API or UI, durable operational state, leases/fencing, production target, service account, action verification/rollback execution, or production authorization. Production actions remain denied.

## Mission Control milestone ledger

| Milestone | Current status | Publication and evidence |
|---|---|---|
| MC-1.5 | Complete | Initial read-only dashboard and service views are in published history and remain visible in the current cockpit |
| MC-2.1 | Complete | Fast/slow telemetry architecture and freshness semantics are published; live telemetry is fresh while MC-3 freshness may be stale |
| MC-3 | Complete for shipped read-only scope | Event/incident processors and projections are published; live events and incidents are observable through bounded GET surfaces |
| MC-4 / MC-4.5 | Complete for disabled-by-default safety scope | Notification decision/outbox/audit foundations are published; provider delivery remains disabled |
| MC-5 | Complete | FastAPI read-only dashboard, façades, static cockpit, and GET-only routes are published |
| MC-5 Gate 2.1 | Passed | Validated staging harness is preserved and protected; no rerun is implied by this document |
| MC-6.1 | Complete | Shared contracts, bounds, state semantics, scheduler, and shell foundation are published |
| MC-6.2 | Complete | Static navigation shell and hash routing are published and visible in the vpanel |
| MC-6.3 | Complete | Dashboard, Server, History, Incidents, and Notifications surfaces are published and live |
| MC-6.4 | Reconciled | Server capability was already delivered through MC-6.3; no duplicate implementation was required |
| MC-6.5 | Complete | Docker/container/project detail façades are published and live |
| MC-6.6 | Complete | Project/application intelligence and refinements 6.6.1–6.6.3 are published and live |
| MC-6.7 / 6.7.1 | Complete for bounded observation | Allow-listed Systemd observation is published; live data has a subset of unavailable/unknown units |
| MC-6.8 | Complete | Bounded redacted logs are published and live |
| MC-6.9 | PASS_EXISTING | Existing evidence/history implementation conforms; its design note remains preserved in the stash rather than published |
| MC-6.10 | Complete under safe posture contract | Settings posture and notification safety are published; `commit=null`/`Unknown` and `not_observed` deployment fields are intentional where no authoritative source exists |
| MC-6.11 | Landed | Read-only Typer/Rich TUI is committed and published; terminal behavior is not verifiable from the public web surface |
| MC-6.12 | Foundation only; operational action plane blocked; service-runtime scopes remediated | Stage 2 and Stage 3 foundations are published; execution remains unimplemented and denied. The service-runtime/telemetry scopes (systemd runtime scopes, project telemetry refresh, dashboard freshness, Git enrichment hardening) are remediated and validated — see [`MC-6.12_TELEMETRY_REMEDIATION.md`](MC-6.12_TELEMETRY_REMEDIATION.md) |
| MC-6.13 Phase 2/3/4A/4B/4C/4C.1/4C.2/4C.3/4D/4E | Complete through bounded read-only Phase 4E | Published advisor domain, transport, fixture/live orchestration, telemetry-owned export/adapter, boundary alignment, complete-evidence validation, and additive resource-history summary |

## Current Git and preservation state

The current main branch contains all approved tracked commits through the seven-file SQLite read-only/journal correction at `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`. The branch is synchronized with `origin/main` and the remote main branch.

The preservation stash `stash@{0}` is intentionally retained. It contains the uncommitted incident-reopen distinct-cycle workstream (`src/aipm/repositories/incidents/sqlite.py` and `tests/test_incident_engine.py`) and the untracked design note `docs/MC-6.9_DESIGN.md`. The stash must not be applied, dropped, rewritten, or silently included in later commits.

The latest local synchronized-checkout validation reproduced 73 affected tests and 618 full-suite tests passing, with one existing Starlette/httpx deprecation warning. A prior operator report cited 629 tests; the discrepancy is not explained by current repository evidence and must not be normalized into a claim.

## Live vpanel status

The read-only public site at [`https://vpanel.03092017.xyz`](https://vpanel.03092017.xyz) was inspected on 2026-08-28 using safe GET/navigation only. `/healthz` returned `{"status":"ok"}`. The dashboard resolved live host data, Docker observations, events, incidents, history, and advisor data. The direct GET `/api/advisor` returned a fresh available host assessment with matching `evaluation_time` and `generated_at`, 18/18 evidence coverage, six valid points across 300 seconds at 60-second cadence for CPU/memory/disk, deterministic metric ordering, and no `maximum_gap` exposure. Zero findings and recommendations in the complete low-pressure case are expected and do not constitute a health claim.

The live site also surfaced bounded degraded states: MC-3 was stale, individual Docker resource observations were stale, some Systemd observations were unavailable/unknown, project aggregate freshness differed between dashboard and Projects views, and notification APIs safely returned unavailable data because delivery is disabled. The events endpoint succeeded with the frontend query `range=24h&limit=50`; a smaller `limit=10` query returned a safe `Invalid event query` response and should be treated as a query-contract edge case.

HTTP observations prove public behavior only. They do not prove the running deployment commit, systemd unit contents, database ownership, producer convergence, filesystem sandbox, or Cloudflare configuration. The live Settings surface reports `commit=Unknown`, `public_ingress=not_observed`, and `permanent_service=not_observed` by design.

## Read-only and action-plane boundaries

> **Mission Control observes the VPS; it does not change the VPS.**

The following boundaries remain in force:

| Boundary | Current requirement |
|---|---|
| HTTP | Mission Control observation routes are GET-only; no acknowledgement, lifecycle, mutation, export, or action route is exposed |
| SQLite | Dashboard reads are read-only/query-only and must not initialize, migrate, checkpoint, repair, merge, delete, or rekey databases |
| Telemetry | Telemetry owns sampling and advisor history selection; no second advisor reader or synthetic/interpolated samples are permitted |
| Notifications | Providers, channels, and delivery remain disabled |
| MC-6.12 | Production actions, executors, action APIs/UI, durable action state, leases/fencing, service accounts, and autonomous remediation remain denied |
| Advisor | Read-only, deterministic, bounded, non-authoritative, no LLM/provider execution, no actions or remediation |
| Databases | `DATABASE_MERGE=NOT_AUTHORIZED`; `DATABASE_DELETE=NOT_AUTHORIZED`; `PRODUCTION_DATA_REPAIR=NOT_AUTHORIZED` |
| Automation | `AUTONOMOUS_REMEDIATION=FORBIDDEN`; `LLM_ASSISTED_EXECUTION=FORBIDDEN`; `PROVIDER_DRIVEN_EXECUTION=FORBIDDEN` |

## Broader AIPM production roadmap

The separate `PRODUCTION_ROADMAP.md` remains incomplete. It covers safe `aipm update` management transactions rather than the read-only Mission Control cockpit. Remaining work includes production-grade update planning, explicit approval, critical Git transaction safety, restore points and rollback, separate planner/executor/verifier services, structured audit history, disposable integration fixtures, CI/release hygiene, and separately approved real-VPS read-only integration. Mission Control completion does not imply completion of that broader product roadmap.

## Documentation governance

This document is the current status authority. Historical documents may retain their original scope and narrative, but each now carries a current-state notice. New status claims must identify whether they are repository evidence, operator-supplied production evidence, or fresh web-observable evidence. No documentation update authorizes code, deployment, service, database, infrastructure, Cloudflare, notification, or action-plane changes.

## Current-state reconciliation — 2026-09-01 (MC-6.12 telemetry/service-runtime remediation)

Repository evidence in this section is verified against the working tree at `ee308ac600f2148166efa1146a455b6bc1fe2a06`.

The MC-6.12 telemetry/service-runtime remediation is implemented, deployed on the VPS, and runtime-validated. It fixes the chain of defects that left the read-only cockpit without project telemetry and with frozen project freshness: the telemetry unit lacked `AIPM_CONFIG` and discovered no projects; Git enrichment ran as `aipm` against `mina`-owned repositories without a per-invocation `safe.directory` exception; one Git enrichment failure previously failed the entire project discovery; the dashboard never re-read persisted project telemetry, so freshness stayed frozen until a dashboard restart; network telemetry required `AF_NETLINK` under sandboxing; and the log-source configuration needed to honor `AIPM_LOG_FILE`.

The remediation is bounded and additive: both long-running units now receive `AIPM_CONFIG`, `AIPM_TELEMETRY_DB`, and `AIPM_LOG_FILE`; the dashboard hydrates persisted project samples at startup and periodically re-reads them through a read-only telemetry-DB connection; Git enrichment uses per-invocation `safe.directory` and degrades per project on failure; no schema migration was introduced. Deployment, rollback, diagnostics, the documented host permission residual (unreadable `aipm` Git ref metadata — degraded gracefully, never auto-repaired), the validated test state (1144 passed, 1 skipped, 2 pre-existing failures unrelated to this patch set), and non-blocking follow-ups are documented in [`MC-6.12_TELEMETRY_REMEDIATION.md`](MC-6.12_TELEMETRY_REMEDIATION.md).

Scope boundaries unchanged by this remediation: the MC-6.12 executor/action plane remains blocked and denied; Mission Control remains a read-only observer; no second project discovery mechanism exists; notification delivery remains disabled. The working tree additionally contains mode-only changes to `ops/migrate-aipm-state.sh`, `ops/setup-aipm-identity.sh`, and `ops/staging/mc5-gate2-staging.sh`, which are pre-existing unrelated working-tree state, not part of the remediation.

## References

[1]: https://github.com/MenaYassa/AIPM/tree/1c1cc4d8839d122f46eb8a1c7592c9c504df68ba "AIPM repository at current checkpoint"

[2]: https://vpanel.03092017.xyz/ "Live AIPM Mission Control"

[3]: https://vpanel.03092017.xyz/api/advisor "Live read-only advisor API"

[4]: LIVE_VPANEL_READONLY_FINDINGS.md "Detailed live vpanel read-only findings"
