# MC-6.12 Control Plane — Operator Transport

## Status

Repository-only, **not deployed**. The transport is an authenticated,
localhost-only HTTP adapter over the control-plane service. It binds to
`127.0.0.1` exclusively; public/unsafe bind addresses are refused at
construction (fail closed). It is never exposed through Cloudflare, nginx, or
any public interface, and it is intentionally separate from the read-only
Mission Control vpanel.

## Security boundary

```
HTTP (loopback only)
  → transport adapter        (no policy / lifecycle / identity / CAS / SQL logic)
  → OwnerControlPlaneService (authorization, confirmation, execution routing)
  → Executor                 (UPDATE_PROJECT_PLAN only, leased + fenced)
  → durable control-plane DB (own SQLite file, hash-chained audit)
```

The transport contains no business logic. Every state change goes through
the service, which owns authorization, confirmation semantics, CAS, and the
canonical audit ledger.

## Endpoint surface

Read-only:

| Route | Purpose |
|---|---|
| `GET /health` | Bounded posture: availability, schema version, audit-chain validity, kill-switch posture |
| `GET /session` | Authenticated session view incl. the CSRF token |
| `GET /plans/{target_id}` | Bounded ProjectPlan view |
| `GET /actions/{action_id}` | Bounded action lifecycle/outcome view |
| `GET /actions/{action_id}/audit` | Audit events referencing one action (bounded) |
| `GET /kill-switch` | Kill-switch posture per environment |
| `GET /audit/verify` | Hash-chain verification (`valid`, `sequence`) |

State-changing (all require an authenticated session **and** the
`X-CSRF-Token` header):

| Route | Purpose |
|---|---|
| `POST /login` | Owner authentication (generic failure; bounded limiter) |
| `POST /logout` | Session revocation (CSRF-protected) |
| `POST /plans/{target_id}/authorize` | Create the bounded action (typed fields only) |
| `POST /actions/{action_id}/confirm` | Owner confirmation + pre-mutation snapshot capture |
| `POST /actions/{action_id}/execute` | Bounded executor (leases, fencing, CAS mutation, independent verification) |
| `POST /actions/{action_id}/reconcile` | UNKNOWN_OUTCOME reconciliation by observation (never a blind retry) |
| `POST /actions/{action_id}/rollback` | Request the rollback action (distinct identity/lease/confirmation) |
| `POST /kill-switch/engage` | Engage the staging kill switch (audited, actor-attributed) |
| `POST /kill-switch/disengage` | Disengage the staging kill switch (audited) |

There are no generic command routes (`/execute-anything`, `/command`,
`/shell`, `/sql`, ...) and no path to UpdateEngine, providers, or subprocess.

## Authentication

Reuses the canonical Argon2id `OwnerAuthenticator` and opaque server-side
`OwnerSessionStore`. `POST /login` with `{"secret": "..."}` sets an opaque
`aipm_cp_session` cookie (`HttpOnly`, `SameSite=Strict`, bounded lifetime;
`Secure` when `secure_cookies=True` — enable behind TLS). The cookie contains
only the opaque session identifier. Failures are generic (`unauthenticated`)
and rate-bounded by the authenticator's built-in limiter; secrets are never
logged, returned, or audited.

## CSRF

The session carries a server-side CSRF token, surfaced only through the
authenticated `GET /session` response. Every state-changing request must
present it as `X-CSRF-Token`; a missing, wrong, or cross-session token is a
403. GET never mutates. CSRF tokens are never written to the audit ledger
(test-enforced).

## Binding

`validate_bind_address()` accepts only IPv4 loopback (`127.0.0.1`);
`0.0.0.0`, `::`, IPv6, and any non-loopback address raise at app construction
and again in `run_operator_transport`. There is no future deployment mode
that relaxes this.

## Errors

One bounded vocabulary (`unauthenticated`, `csrf_failed`, `invalid_request`,
`not_found`, `conflict`, `stale_plan`, `expired`, `kill_switch_*`,
`execution_refused`, ...) with predictable status codes (401/403/404/405/409/
410/422/423/500). Responses never include stack traces, SQL, filesystem
paths, or internal representations.

## Operator flow example

```text
POST /login                       {"secret": "..."}
GET  /session                     → csrf_token
POST /plans/demo/authorize        {"fields": {"title": "New"}, "idempotency_key": "op-1"}
POST /actions/{id}/confirm        (X-CSRF-Token) → confirmation consumed + snapshot captured
POST /actions/{id}/execute        (X-CSRF-Token) → verified_success
GET  /actions/{id}                → durable state
GET  /audit/verify                → {"valid": true, ...}
```

Failure path: execute with a forced mismatch → `verification_failed` →
`POST /actions/{id}/rollback` → confirm the rollback action → execute it →
original `rolled_back`, plan restored, chain valid.

## Operational limitations

- Single-owner; sessions are process-local (durable sessions are future work).
- Plain-HTTP loopback by default; enable `secure_cookies` only behind TLS.
- `GET /health` reports transport/control-plane posture only — not a
  statement about the VPS.
- Rate limiting is a bounded sliding window on authorize/confirm (30/min);
  login abuse is bounded by the authenticator's lockout.
- Not deployed, not reverse-proxied, not registered as a systemd unit.
