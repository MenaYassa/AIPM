# AIPM Current Status

**Status date:** 2026-09-03 (supersedes the 2026-09-02 reconciliation; see the final section)

**Canonical repository checkpoint:** `c04c017efa21540082783fc4377f2b50ce3c86a3` (published `origin/main`; carries the MC-6.12 transport rate-limit regression tests and the telemetry remediation lineage)

**Repository parity:** `HEAD` is `origin/main` at `c04c017efa21540082783fc4377f2b50ce3c86a3`.

## Purpose of this document

This is the canonical current-state reconciliation for the AIPM repository and Mission Control. Historical design and completion documents remain preserved as audit records, but their older checkpoint and “planned/future” wording must be interpreted through this document. The detailed read-only public inspection is preserved in [`LIVE_VPANEL_READONLY_FINDINGS.md`](LIVE_VPANEL_READONLY_FINDINGS.md).

## Executive status

The read-only Mission Control cockpit is substantially landed and live. The public vpanel provides functioning read-only surfaces for the dashboard, host/server intelligence, Docker, projects, Systemd observation, bounded logs, incidents, history, settings posture, and the advisor. MC-6.13 is complete through Phase 4E for its explicitly bounded read-only advisor scope.

The repository is fully committed and published at the checkpoint above. This does not mean that every historical workstream is committed: the preservation stash still contains the separate incident-reopen workstream and `docs/MC-6.9_DESIGN.md`. Those items were intentionally not merged into current main.

MC-6.12 is **not operationally complete**. Current main contains Stage 2 pure control-plane models, the Stage 3 staging-only process-local owner authentication/session/project-plan foundation, and the MC-6.12 execution-plane release (`a8559b4d600fff456f84200cec81a93e60f848d6`, 2026-08-31): a durable SQLite control-plane store, bounded authorization/lifecycle/audit/verification/rollback behavior, the localhost-only operator transport (session cookies, CSRF, bounded 429 rate limiting on authorize/confirm, loopback bind validation), and a durable staging kill switch seeded engaged. The executor path remains unimplemented and denied: production actions are refused (`execution_mode` must be explicitly `"ipc"` for production and no production IPC executor is deployed), the production kill switch is permanently engaged, plans are hard-restricted to staging with the bounded `{title, objective}` field allow-list, and no action API/UI is exposed to the public vpanel. Production authorization remains denied.

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
| MC-6.12 | Staging control plane landed; operational action plane still blocked; service-runtime scopes remediated | Stage 2 and Stage 3 foundations are published; the staging-only execution-plane release (`a8559b4d600fff456f84200cec81a93e60f848d6`) is published — durable control-plane store, verification/rollback contracts, localhost operator transport, staging kill switch — while executor/production paths remain unimplemented and denied. The service-runtime/telemetry scopes (systemd runtime scopes, project telemetry refresh, dashboard freshness, Git enrichment hardening) are remediated and validated — see [`MC-6.12_TELEMETRY_REMEDIATION.md`](MC-6.12_TELEMETRY_REMEDIATION.md) |
| MC-6.13 Phase 2/3/4A/4B/4C/4C.1/4C.2/4C.3/4D/4E | Complete through bounded read-only Phase 4E | Published advisor domain, transport, fixture/live orchestration, telemetry-owned export/adapter, boundary alignment, complete-evidence validation, and additive resource-history summary |

## Current Git and preservation state

The current main branch contains all approved tracked commits through the seven-file SQLite read-only/journal correction at `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`, the MC-6.12 privilege/identity hardening at `ee308ac600f2148166efa1146a455b6bc1fe2a06`, and the MC-6.12 execution-plane release at `a8559b4d600fff456f84200cec81a93e60f848d6` (see the milestone ledger). The branch is synchronized with `origin/main` and the remote main branch; `origin/main` currently resolves to `43e440316df8db3721b3815983228b7d6939585b` (the MC-6.12 telemetry/service-runtime remediation commit, which HEAD matches).

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
| MC-6.12 | Production actions, production executors, public action APIs/UI, production durable action state, production leases/fencing, service accounts, and autonomous remediation remain denied; the staging-only control plane (durable state, operator transport, verification/rollback, staging kill switch) is landed and published |
| Advisor | Read-only, deterministic, bounded, non-authoritative, no LLM/provider execution, no actions or remediation |
| Databases | `DATABASE_MERGE=NOT_AUTHORIZED`; `DATABASE_DELETE=NOT_AUTHORIZED`; `PRODUCTION_DATA_REPAIR=NOT_AUTHORIZED` |
| Automation | `AUTONOMOUS_REMEDIATION=FORBIDDEN`; `LLM_ASSISTED_EXECUTION=FORBIDDEN`; `PROVIDER_DRIVEN_EXECUTION=FORBIDDEN` |

## Broader AIPM production roadmap

The separate `PRODUCTION_ROADMAP.md` remains incomplete. It covers safe `aipm update` management transactions rather than the read-only Mission Control cockpit. Remaining work includes production-grade update planning, critical Git transaction safety hardening, separate planner/executor/verifier services with presentation decoupling, disposable integration fixtures, CI/release hygiene, and separately approved real-VPS read-only integration. The P1 **restore points and deterministic rollback** workstream is now implemented at repository level: `RollbackManager`/`RestoreResult` (`src/aipm/services/update/rollback.py`, `src/aipm/models/rollback.py`) restore project files from a `BackupEngine` snapshot archive without deleting anything, keep `.git`, volumes, databases, and runtime state out of scope, fail closed on unsafe archive members (symlinks, traversal, non-regular files), and the update engine's failure path automatically restores from the pre-update snapshot with the restore outcome recorded in the audit record (`UpdateAudit.restore`). Regression coverage lives in `tests/test_update_rollback.py` (10 tests). The P1 **independent update verifier** is also implemented at repository level: `UpdateVerifier`/`UpdateVerification` (`src/aipm/services/update/verifier.py`, `src/aipm/models/verification.py`) is a distinct component from planning and execution that compares health-before and health-after reports and returns a typed success/warning/failure verdict (warnings are explicitly not rollback conditions; critical findings or an unevaluatable post-update state fail safe and enter the same rollback path as an execution failure), with the verdict recorded additively in `UpdateAudit.verification`. Regression coverage lives in `tests/test_update_verifier.py` (11 tests). Mission Control completion does not imply completion of that broader product roadmap.

## Documentation governance

This document is the current status authority. Historical documents may retain their original scope and narrative, but each now carries a current-state notice. New status claims must identify whether they are repository evidence, operator-supplied production evidence, or fresh web-observable evidence. No documentation update authorizes code, deployment, service, database, infrastructure, Cloudflare, notification, or action-plane changes.

## Current-state reconciliation — 2026-09-03 (production roadmap: restore points and deterministic rollback)

Repository evidence in this section is verified against the working tree at `c04c017efa21540082783fc4377f2b50ce3c86a3` (`HEAD` == `origin/main`), plus the unstaged work described here.

The roadmap position after MC-6.12 remains unchanged: MC-6.12 operational completion (executor/production action plane) and MC-6.13 post-4E remain separately authorized and denied, so the next authorized workstream is the broader production roadmap's safe `aipm update` transaction. Within it, the P1 "Explicit rollback and restore points" item is now implemented at repository level: `RollbackManager` (`src/aipm/services/update/rollback.py`) restores project files from a `BackupEngine` snapshot archive per an explicit contract — restores exactly the regular files captured in the snapshot, never deletes anything, reports files created after the snapshot as left in place, keeps `.git`, `BackupEngine.DEFAULT_EXCLUDES` directories, volumes, databases, and runtime state out of scope, and fails closed on non-regular, symlink, path-traversal, or foreign-prefix archive members. `RestoreResult` (`src/aipm/models/rollback.py`) is the typed outcome (attempted/success, restored, left_in_place, skipped, error). The update engine's failure path attempts an automatic restore from the pre-update snapshot when a snapshot exists, prints the restore outcome, records it in `UpdateAudit.restore` (additive audit field), and never masks the original failure. Dry-run, blocked, and approval-denied paths never restore.

Validation: `tests/test_update_rollback.py` (10 tests) passes; the full suite is 1158 passed, 1 skipped, 2 failed — the same two documented pre-existing failures (`tests/test_mc612_stage15_privilege.py::test_assert_detects_human_session_without_executor_rule`, `tests/test_mc612_stage25a_identity_setup.py::test_s6_privileged_group_contamination_fails`). Still open in the broader roadmap: production-grade update planning, Git transaction safety hardening, separate planner/executor/verifier services with Rich decoupling, disposable integration fixtures, CI/release hygiene, and separately approved real-VPS read-only integration (blocked on credentials/approval). This reconciliation is repository evidence only; no deployment, service, database, or action-plane state was changed. See the dated reconciliation in [`PRODUCTION_ROADMAP.md`](../PRODUCTION_ROADMAP.md) for the same record.

## Current-state reconciliation — 2026-09-03 (production roadmap: independent update verifier)

Repository evidence in this section is verified against the working tree at `fabb6e64edff8572a468e69de3e67974e401e505` (`HEAD` == `origin/main`), plus the unstaged verifier work described here.

Within the safe `aipm update` transaction workstream, the P1 "Update execution and verification" verifier item is now implemented at repository level: `UpdateVerifier` (`src/aipm/services/update/verifier.py`) is a distinct component from planning and execution, injected into the update engine, which consumes its typed verdict `UpdateVerification` (`src/aipm/models/verification.py`: status `success`/`warning`/`failure`, plus passed/warnings/failures findings and a fail-safe error field) instead of declaring success itself. Verdict semantics per the roadmap: SUCCESS = no warning-or-worse post-update findings; WARNING = high/warning findings without critical ones and is explicitly not a rollback condition (the update remains successful); FAILURE = critical post-update findings — the same condition that already drove the engine's rollback path — or an unevaluatable post-update state (missing health-after report, unexpected verifier error), which fails safe. A verification FAILURE enters the same rollback path as an execution failure: pre-update snapshot restored, and the audit record (`UpdateAudit.verification`, additive) preserves execution result, verification result, and restore result (`UpdateAudit.restore`) as distinct fields. Verification is read-only with respect to the project (it analyzes already-produced health reports; no commands, no mutation). Validation: `tests/test_update_verifier.py` (11 tests) passes; full suite 1169 passed, 1 skipped, 2 failed — the same two documented pre-existing failures above. This remains repository-only work: implemented and tested in the repository, **not deployed**, no live-VPS validation; the MC-6.12 executor and production action execution remain unauthorized and unimplemented. See the dated reconciliation in [`PRODUCTION_ROADMAP.md`](../PRODUCTION_ROADMAP.md) for the same record.

## Current-state reconciliation — 2026-09-02 (control-plane state correction)

Repository evidence in this section is verified against the working tree at `43e440316df8db3721b3815983228b7d6939585b`.

The 2026-09-01 reconciliation below described the MC-6.12 telemetry/service-runtime remediation as present in the working tree but not yet committed. That wording is now stale: the remediation was committed as `43e440316df8db3721b3815983228b7d6939585b` and published to `origin/main`, and the working tree is clean except for pre-existing mode-only changes to `ops/migrate-aipm-state.sh`, `ops/setup-aipm-identity.sh`, and `ops/staging/mc5-gate2-staging.sh`, which are unrelated working-tree state and remain unstaged.

The same reconciliation understated the landed control-plane surface: the MC-6.12 execution-plane release `a8559b4d600fff456f84200cec81a93e60f848d6` (2026-08-31) is in published history and contains the staging-only control plane — a durable SQLite control-plane store (plans, decisions, actions, leases, snapshots, verification evidence, kill-switch state), bounded `{title, objective}` ProjectPlan mutations with compare-and-set revisions and digests, independent read-back verification, CAS-guarded rollback against immutable pre-mutation snapshots, a hashed audit chain, the localhost-only authenticated operator transport (Argon2id owner login, server-side sessions, CSRF on state changes, bounded 429 rate limiting on authorize/confirm), and a durable kill switch seeded engaged for staging and permanently engaged for production. The executor remains unimplemented and denied, production authorization is denied, and no action surface is exposed on the public vpanel; MC-6.12 remains not operationally complete. The transport rate-limiting behavior is additionally covered by dedicated tests added under this reconciliation (30 requests/minute/session windows on authorize and confirm returning 429).

## Current-state reconciliation — 2026-09-01 (MC-6.12 telemetry/service-runtime remediation)

Repository evidence in this section is verified against the working tree at `ee308ac600f2148166efa1146a455b6bc1fe2a06`. Historical statements in this section about commit/publish state are superseded by the 2026-09-02 section above.

The MC-6.12 telemetry/service-runtime remediation is implemented, deployed on the VPS, and runtime-validated. It fixes the chain of defects that left the read-only cockpit without project telemetry and with frozen project freshness: the telemetry unit lacked `AIPM_CONFIG` and discovered no projects; Git enrichment ran as `aipm` against `mina`-owned repositories without a per-invocation `safe.directory` exception; one Git enrichment failure previously failed the entire project discovery; the dashboard never re-read persisted project telemetry, so freshness stayed frozen until a dashboard restart; network telemetry required `AF_NETLINK` under sandboxing; and the log-source configuration needed to honor `AIPM_LOG_FILE`.

The remediation is bounded and additive: both long-running units now receive `AIPM_CONFIG`, `AIPM_TELEMETRY_DB`, and `AIPM_LOG_FILE`; the dashboard hydrates persisted project samples at startup and periodically re-reads them through a read-only telemetry-DB connection; Git enrichment uses per-invocation `safe.directory` and degrades per project on failure; no schema migration was introduced. Deployment, rollback, diagnostics, the documented host permission residual (unreadable `aipm` Git ref metadata — degraded gracefully, never auto-repaired), the validated test state (1144 passed, 1 skipped, 2 pre-existing failures unrelated to this patch set), and non-blocking follow-ups are documented in [`MC-6.12_TELEMETRY_REMEDIATION.md`](MC-6.12_TELEMETRY_REMEDIATION.md).

Scope boundaries unchanged by this remediation: the MC-6.12 executor/action plane remains blocked and denied; Mission Control remains a read-only observer; no second project discovery mechanism exists; notification delivery remains disabled. The working tree additionally contains mode-only changes to `ops/migrate-aipm-state.sh`, `ops/setup-aipm-identity.sh`, and `ops/staging/mc5-gate2-staging.sh`, which are pre-existing unrelated working-tree state, not part of the remediation.

## References

[1]: https://github.com/MenaYassa/AIPM/tree/1c1cc4d8839d122f46eb8a1c7592c9c504df68ba "AIPM repository at current checkpoint"

[2]: https://vpanel.03092017.xyz/ "Live AIPM Mission Control"

[3]: https://vpanel.03092017.xyz/api/advisor "Live read-only advisor API"

[4]: LIVE_VPANEL_READONLY_FINDINGS.md "Detailed live vpanel read-only findings"
