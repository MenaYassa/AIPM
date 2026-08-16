# Mission Control MC-4: Alerts and Notifications

MC-4 adds incident-aware notifications without changing the meaning of telemetry, events, incidents, health findings, or severity. The system uses a durable SQLite outbox and a separate `aipm notifications run` worker.

## Runtime flow

```text
TelemetrySampler → EventProcessor → IncidentEngine
                                      ↓
                          incident transition audit
                                      ↓
                    notification policy and deduplication
                                      ↓
                              SQLite outbox
                                      ↓
                           notification worker
                                      ↓
                            channel adapters
```

The event processor does not make network calls. It persists a typed incident transition in the same SQLite database boundary as the incident lifecycle update. The notification worker projects unprocessed transitions into deterministic notification identities and delivery rows, claims deliveries with a short lease, calls an adapter outside the database transaction, then records the outcome and attempt audit.

## Safety boundary

Notification credentials are referenced by environment-variable name only. Values never enter source control, SQLite, API responses, browser payloads, or logs. MC-4 is read-only toward VPS infrastructure; its only external side effect is sending an explicitly configured notification. It does not execute Docker, Compose, Git, systemd, Cloudflare, package management, remediation, or AI analysis.

## Configuration

The sample configuration keeps notifications disabled until the operator explicitly enables them. Channel entries use `secret_ref` and `destination_ref`, such as `AIPM_NOTIFY_TELEGRAM_TOKEN` and `AIPM_NOTIFY_TELEGRAM_CHAT_ID`; the YAML does not contain their values.

Policies match existing `Severity`, `EventType`, `ResourceType`, and incident transition values. Incident openings and escalations are the only default transition semantics. Recovery and acknowledgement are independently controlled. Repeated observations in one incident are suppressed unless update notifications are explicitly enabled.

## Rate protection

Every notification identity is deterministic across policy, channel, incident, transition, and transition ID. A unique identity prevents duplicate projection. Policy cooldowns, fixed-window limits, and per-channel limits are the required protection against the 15-second telemetry cadence creating an external notification storm.

## Delivery states

Deliveries move through `pending`, `sending`, `sent`, `failed`, and `unknown`. Leases recover rows left in `sending` after a worker crash. Retryable failures use bounded exponential backoff. An ambiguous provider timeout is represented as `unknown` rather than blindly retried, because exactly-once external delivery cannot be guaranteed without provider idempotency or reconciliation.

## SQLite operations

The initial deployment supports one telemetry sampler, one event processor, one notification worker, and the dashboard using short transactions with WAL mode. Network calls never occur while a transaction is open. PostgreSQL and a queue should be evaluated when sustained lock contention, multiple required workers, or notification backlog age makes the single-worker SQLite topology insufficient.

## API

The dashboard adds safe read-only routes:

```text
GET /api/notifications
GET /api/notifications/{id}
GET /api/notification-channels
GET /api/notification-policies
```

Responses include statuses, IDs, policy/channel names, incident references, safe error codes, and timestamps. They do not include secrets, secret references, authorization headers, provider payloads, or destination tokens.

## CLI

```text
aipm notifications list
aipm notifications retry <notification-id>
aipm notifications test <channel-id> [--yes]
aipm notifications run
```

The worker remains disabled unless `notifications.enabled` is true. The test command is deliberately a guarded boundary and does not send a real message in the initial adapter slice.

## Testing

MC-4 tests use temporary SQLite databases and mocked adapters. They cover typed policy matching, recovery suppression, outbox idempotency, worker success, retryable failure, and safe delivery context. No test calls Telegram, email, webhook, Cloudflare, Docker, Git, or a production VPS.
