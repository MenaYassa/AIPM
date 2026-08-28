# MC-6.1 Shared Contracts and UI Foundation

> **Current-state notice — 2026-08-28:** This document is retained as part of the AIPM documentation record. Its historical design or milestone narrative remains valid as historical context, but current completion, publication, deployment, and live-observation claims are superseded by [`docs/CURRENT_STATUS.md`](CURRENT_STATUS.md) and [`docs/LIVE_VPANEL_READONLY_FINDINGS.md`](LIVE_VPANEL_READONLY_FINDINGS.md). The current tracked repository is synchronized at `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`; MC-6.12 operational execution remains blocked, and the incident-reopen workstream remains preserved separately in `stash@{0}`.


## Scope

MC-6.1 establishes reusable read-only contracts and frontend scheduling boundaries without expanding Mission Control into Systemd, Logs, TUI, actions, authentication, public ingress, notifications, or AI Agent functionality.

The implementation deliberately reuses the existing `FreshnessStatus`/`TelemetryFreshness` concepts for current telemetry while adding a cross-domain `Observation` contract for future façades. Existing MC-5 routes and response payloads remain unchanged.

## Observation contract

`aipm.models.mission_control.Observation` distinguishes four dimensions that must not be collapsed:

| Dimension | Meaning |
|---|---|
| `transport_ok` | Whether the adapter/request completed successfully. |
| `available` | Whether a usable observation exists. |
| `state` | `fresh`, `stale`, `unavailable`, `never_sampled`, `unknown`, or `error`. |
| `error` | Safe structured code/message, without tracebacks, secrets, SQL, or provider payloads. |

`Observation.from_sample()` requires timezone-aware timestamps and computes bounded age/freshness deterministically. Existing dashboard mappers are not rewritten in MC-6.1.

## Query bounds

`aipm.capabilities.dashboard.query_bounds` provides reusable validation for supported history ranges, limits, offsets, cursors, filters, future log-line limits, and future log-byte limits. It does not implement log access. Existing history/event services retain their established contracts; the helper is available for future additive façades and tests.

## Frontend modules

The current vanilla frontend now imports two small static modules:

- `mission-control-state.js` normalizes observation states and preserves existing visual classes.
- `mission-control-scheduler.js` owns one timer per resource, prevents overlapping loads, provides bounded retry delay, supports visibility pause/resume, permits manual refresh without timer creation, and cleans up on page exit.

The existing sections, styles, routes, filters, empty/error states, and effective polling cadence remain:

| Resource | Cadence |
|---|---:|
| Overview | 15 seconds |
| Service Pulse | 15 seconds |
| MC-3 Events | 15 seconds |
| History | 60 seconds/manual |
| Incidents | 30 seconds |
| Notifications | 30 seconds |

No frontend build system or framework was introduced.

## Safety fixture scanner

`aipm.capabilities.dashboard.safety` provides a conservative JSON-like payload scanner for MC-6.1 contract tests. It rejects secret-like keys, PEM key material, external URLs, credential-like values, and destination-like fields while allowing loopback URLs used by local dashboard contracts. It does not access environments, databases, providers, or live filesystems.

## Validation

MC-6.1 adds Python foundation tests and deterministic Node scheduler tests. The existing MC-5 suite and full repository suite must remain green. Static checks must confirm no mutation routes, no legacy frontend timers, no secret fixture leakage, and no production/runtime file changes.

## Explicitly not included

Systemd observation, Logs, TUI, Docker lifecycle actions, Git updates, shell execution, Compose mutation, service control, incident acknowledgement, notification activation/sending, settings mutation, authentication, public ingress, Cloudflare changes, SSE, WebSockets, AI Agent integration, remediation, new databases, new workers, and new production services remain outside MC-6.1.
