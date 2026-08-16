# Mission Control MC-4 Completion Report

## Scope

MC-4 adds incident-aware Alerts & Notifications to AIPM. It preserves MC-2 telemetry and MC-3 event/incident semantics and stops before MC-5 Guarded Operations and MC-6 AI Advisor.

## Implemented

The implementation includes typed notification models and configuration, durable incident-transition audit records, SQLite notification projection/outbox tables, delivery leases and attempt audit, deterministic policy matching, deduplication identities, safe suppression decisions, bounded retry behavior, and explicit `unknown` outcomes for ambiguous network results.

The channel boundary includes a standard-library HTTP/webhook adapter, Telegram adapter, unconfigured adapter fallback, and a registry that can be replaced by mocked adapters in tests. Secrets are resolved from environment-variable references only at adapter invocation and are not serialized or logged.

The dedicated `aipm notifications run` process performs projection and delivery independently from `aipm events run`. The CLI also provides read-only listing, a guarded retry inspection boundary, and a deliberately non-sending channel test boundary. The dashboard provides read-only notification, channel, and policy routes and a notification audit panel.

## Files and documentation

The design and operational details are in [`MC-4_ARCHITECTURE.md`](MC-4_ARCHITECTURE.md). The sample configuration keeps notifications disabled by default and contains only environment-variable names, never secret values.

## Verification

The complete test suite passes with **73 tests passed**. This includes the 69 pre-MC-4 regression tests plus focused MC-4 tests for policy matching, recovery suppression, temporary SQLite outbox idempotency, mocked adapter success, and retryable failure. Python source compilation, CLI help registration, and `git diff --check` also pass.

No test uses a production VPS, real Telegram, email, webhook, Cloudflare, Docker, Git remote, or external notification service. No package was installed and no systemd, Cloudflare, or VPS configuration was changed.

## Production notes

The initial worker topology is one telemetry sampler, one event processor, one notification worker, and the dashboard using the shared SQLite database with short transactions and WAL. Network calls occur outside transactions. PostgreSQL plus a queue should be evaluated when sustained SQLite lock contention, multiple required workers, or notification backlog age makes the single-worker topology insufficient.

## Explicit boundary

MC-4 performs no remediation and does not start, stop, restart, update, or mutate VPS infrastructure. It does not change incident state. The implementation ends here; MC-5 and MC-6 require separate explicit approval.
