# MC-2.1 Telemetry Performance & Sampling Runbook

> **Current-state notice — 2026-08-28:** This document is retained as part of the AIPM documentation record. Its historical design or milestone narrative remains valid as historical context, but current completion, publication, deployment, and live-observation claims are superseded by [`docs/CURRENT_STATUS.md`](CURRENT_STATUS.md) and [`docs/LIVE_VPANEL_READONLY_FINDINGS.md`](LIVE_VPANEL_READONLY_FINDINGS.md). The current tracked repository is synchronized at `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`; MC-6.12 operational execution remains blocked, and the incident-reopen workstream remains preserved separately in `stash@{0}`.


## Scope

MC-2.1 separates telemetry into a fast state path and independently scheduled slow refresh tasks. The fast path retains the existing 15-second cadence and never waits for Docker resource statistics or project discovery. Docker resource collection is performed through one provider-bound aggregate `docker stats --no-stream` operation, initially every 60 seconds with a 15-second timeout. Project discovery is independently refreshed every 60 seconds.

The implementation remains read-only toward VPS infrastructure. It does not start, stop, restart, remove, prune, exec into, or mutate containers, Git repositories, Compose projects, systemd, Cloudflare, packages, or notifications.

## Configuration

```yaml
telemetry:
  enabled: true
  interval_seconds: 15
  resource_sampling_enabled: true
  resource_interval_seconds: 60
  resource_timeout_seconds: 15
  resource_stale_after_seconds: 180
  project_interval_seconds: 60
  project_timeout_seconds: 15
  slow_task_max_concurrency: 1
  sampling_mode: split
```

`sampling_mode: split` is the normal MC-2.1 path. `sampling_mode: legacy` retains the pre-MC-2.1 synchronous behavior as a rollback/diagnostic switch. Slow-task concurrency is intentionally fixed at one per slow task category; the coordinator never queues overlapping copies.

## Data freshness

Resource and project values are typed as `fresh`, `stale`, `unavailable`, or `never_sampled`. Existing overview fields remain available for compatibility, but additive freshness metadata reports `sampled_at`, `age_seconds`, `status`, `max_age_seconds`, and a safe error string. Stale values are last-known values and must not be interpreted as current measurements.

The compatibility `container_samples` table retains existing fields and gains additive freshness columns. Sparse canonical resource history is stored in `resource_sample_runs` and `container_resource_samples`. Existing MC-2 rows are migrated additively without synthetic backfill or destructive deletion.

## Commands and endpoints

The existing commands remain available:

```text
aipm telemetry sample
 a single fast state sample in split mode

aipm telemetry resource-sample
 one explicit bounded aggregate resource refresh

aipm telemetry run
 the persistent coordinator-managed telemetry process
```

Existing `/api/overview` and `/api/history/{host,containers,projects,tunnel}` contracts are preserved. The additive endpoint `/api/history/container-resources` exposes sparse resource history with the same bounded range and limit validation.

## Acceptance verification

The implementation is verified with temporary SQLite databases and mocked providers. The test suite proves that:

- The fast snapshot performs no per-container Docker stats calls.
- Aggregate resource collection performs one provider-bound aggregate call.
- Freshness transitions are explicit and stale age is recomputed on each fast snapshot.
- An 85-second-equivalent slow resource operation does not delay fast telemetry.
- Slow resource work is single-flight and records skipped cadence attempts.
- Timeout state is recorded without starting a second worker.
- Legacy MC-2 container rows migrate without data loss.
- Sparse resource history persists and queries correctly.
- Resource-only changes do not emit MC-3 lifecycle events.
- Existing event keys and event derivation tests remain unchanged.
- Invalid cadence, timeout, freshness, concurrency, and sampling-mode values fail at startup.

Measured repository verification at implementation time:

| Verification | Result |
|---|---:|
| Full test suite | **106 passed**, 1 pre-existing dependency deprecation warning |
| MC-2.1 focused tests | **11 passed** |
| MC-3 event derivation plus MC-2.1 compatibility | **17 passed** |
| Python compilation | Passed |
| Whitespace audit | Passed |
| Real Docker/Git/Cloudflare/VPS operations | None performed |

The supplied target-VPS baseline remains the performance comparison point: approximately 86.469 seconds for the old full snapshot with 43 sequential Docker stats calls, versus approximately 2.171 seconds for the measured aggregate Docker stats operation. A target-VPS performance run is still required before declaring the numerical p95 acceptance gates complete; this implementation does not modify or access the VPS.

## Rollback

Set `telemetry.sampling_mode` to `legacy` and restart only the operator-managed telemetry process through the existing deployment mechanism. The additive tables and rows are retained. Database rollback is a verified file-level restore, not a destructive reverse migration. The legacy path is a fallback and is not recommended as steady-state operation on the measured VPS.

## Explicit boundary

MC-2.1 ends with this telemetry architecture, compatibility layer, tests, and runbook. No new resource-threshold events, incidents, notifications, guarded operations, remediation, AI advisor, Cloudflare account access, MC-3.x, MC-4.x, MC-5, or MC-6 work is included.
