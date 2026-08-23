# MC-4.5 Production Hardening Runbook

## Readiness status

MC-4.5 hardens the notification subsystem but does not authorize notification production enablement by itself. The dashboard remains loopback-bound, while the current public ingress is an existing bridge path serving `vpanel.03092017.xyz`:

```text
Cloudflared container
  ↓
172.20.0.1:8788
  ↓
host nginx reverse proxy
  ↓
127.0.0.1:8787
  ↓
AIPM Mission Control
```

AIPM is not an authentication provider. Do not bind the dashboard to `0.0.0.0` for convenience, do not change Cloudflared or Docker configuration from this runbook, and do not treat a tunnel hostname as authorization beyond the existing ingress/access policy.

## Safe staging verification

Perform staging verification with a temporary database, notifications disabled or pointed at mocked adapters, and no production credentials. Verify the telemetry sampler, event processor, notification worker, and dashboard independently. Send no external notification.

| Check | Expected result |
|---|---|
| Dashboard bind | Default remains `127.0.0.1:8787`. |
| Notification configuration | Disabled by default in staging unless a mock channel is explicitly enabled. |
| Worker restart | Pending rows remain durable and are reclaimed after restart. |
| SIGTERM | Worker exits after the current bounded operation without mutating infrastructure. |
| Retry limit | Retryable failures become terminal `FAILED` at channel `max_attempts`. |
| Cooldown/rate limit | Repeated transitions are persisted as suppression audit records. |
| UNKNOWN | Unknown delivery is not automatically retried; reconciliation is explicit. |
| Lease recovery | Expired lease is claimed by a replacement worker; stale owner cannot finish it. |
| Database recovery | Temporary backup restores identities, leases, statuses, and unique keys. |
| Logs | Delivery, suppression, retry, unknown, terminal failure, and retention outcomes are visible without secrets. |
| Environment file | If used by systemd, permissions are restrictive and the service user can read it. |
| Public ingress | Existing Cloudflared container → `172.20.0.1:8788` → host nginx → `127.0.0.1:8787`; any ingress change remains separately approved. |

## Systemd review checklist

MC-4.5 does not mutate systemd. When an operator reviews a staging unit, verify that the notification service is a single worker for the shared database, uses `Restart=on-failure`, has a bounded restart delay, runs as a dedicated least-privilege service user, reads only the protected environment file, and starts after the event process for operational clarity. The worker must remain able to catch up if the event process is delayed.

No systemd unit should pass credentials on a command line. The unit must not grant Docker control or shell-remediation permissions solely for notification delivery.

## Retry and UNKNOWN operations

Automatic retry is bounded by each channel’s `max_attempts`. When the limit is reached, the delivery is terminal `FAILED`, the final attempt remains auditable, and the notification is not claimed again by the normal worker.

An `UNKNOWN` result means the provider outcome is ambiguous. It is not silently converted to `FAILED` and is not automatically retried. If the provider supports lookup or idempotency reconciliation, confirm the provider outcome first. Otherwise, use the explicit operator decision:

```text
aipm notifications reconcile NOTIFICATION_ID --delivered --yes
aipm notifications reconcile NOTIFICATION_ID --not-delivered --yes
aipm notifications retry NOTIFICATION_ID --yes
```

Retries preserve the original notification identity and record an operator action. Manual retries are bounded and cannot be used to retry a successful notification.

## Retention and backup

Retention is timestamp-based and runs through the notification worker maintenance cadence or the explicit command:

```text
aipm notifications retain
aipm notifications metrics
```

Pending, sending, unknown, and notifications associated with open or acknowledged incidents are preserved. Terminal records outside the configured retention window may be removed together with eligible attempts and actions. Do not run retention against production until the database backup has been verified.

The shared SQLite database contains telemetry, events, incidents, incident transitions, notification decisions, deliveries, attempts, suppression audit, and operator actions. A safe backup/restore review must preserve unique event, incident, notification, and provider-request identities; delivery status; leases; pending rows; and foreign-key integrity. Use a copied temporary database only. Never overwrite the live database during a smoke test.

## Metrics and logs

The read-only dashboard endpoint is:

```text
GET /api/notification-metrics
```

It exposes pending, sending, failed, unknown, suppressed, and sent counts; oldest pending and unknown ages; retry exhaustion; lease expiry; recent latency; and per-channel outcomes. The CLI provides the same safe operational view.

Logs may include notification ID, incident ID, policy ID, channel ID, attempt number, safe error code, suppression reason, and retention counts. Logs must never contain tokens, passwords, authorization headers, provider credentials, or raw sensitive provider payloads.

## Production enablement gate

Keep notification delivery disabled until all of the following are verified:

1. P0 retry exhaustion and storm-protection tests pass.
2. Protected public ingress is verified independently of AIPM.
3. Staging worker restart, SIGTERM, lease recovery, database restore, and one-worker behavior pass.
4. Enabled channels are supported and have validated destination/secret references.
5. Storage volume and retention behavior are measured with representative data.
6. Operators understand UNKNOWN reconciliation and bounded retry procedures.

MC-4.5 remains read-only toward VPS infrastructure. It does not restart containers, execute commands, mutate systemd, change Cloudflare, modify Git projects, install packages, remediate incidents, or use AI. After this milestone, stop before MC-5 and MC-6.
