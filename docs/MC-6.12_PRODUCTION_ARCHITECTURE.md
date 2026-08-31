# MC-6.12 Production Architecture

**Status:** repository-only; nothing deployed. External mutation disabled by default. This document is the architectural source of truth for the control plane and its execution plane.

## Planes

```
CONTROL PLANE (authority)                    EXECUTION PLANE (mechanism)
────────────────────────────                 ───────────────────────────
OwnerAuthenticator        (Argon2id)         ExecutionContract   (immutable, digest-bound)
OwnerSessionStore         (durable)          Executor            (bounded, one capability)
AuthorizationPolicy       (deny-by-default)  CapabilityRegistry  (typed, fail-closed)
OwnerConfirmationService  (single-use)       ExecutorRegistry    (explicit mapping)
AuditLedger               (hash-chained)     RecoveryManager     (durable, no blind retry)
KillSwitchRegistry        (durable, staged)  TargetRecord        (resolved binding)
```

The execution plane never imports authentication, sessions, transport, or
Mission Control (source-scan enforced). It receives typed contracts and
returns typed results. The control plane remains the sole authority for
authorization, policy, confirmation, lifecycle, audit, and the kill switch.

## Identity & sessions

Owner authentication is Argon2id via the canonical verifier (fail-closed).
Sessions are opaque 256-bit tokens; the cookie carries the raw token, the
database stores only SHA-256(token) plus principal subject, auth epoch,
validity windows, and revocation. Revocation and epoch rotation survive
restart. Session rotation on login issues a fresh identifier and revokes the
old one (fixation defense). CSRF tokens are per-session random values never
written to the audit ledger.

## Action lifecycle

```
REQUESTED → PLANNED → CONFIRMATION_REQUIRED → CONFIRMED → SNAPSHOT_CAPTURED
→ LEASED → RUNNING → EXECUTED_PENDING_VERIFICATION → VERIFIED_SUCCESS
                                     ↘ VERIFICATION_FAILED → ROLLBACK_REQUESTED
                                         → ROLLED_BACK / ROLLBACK_FAILED
RUNNING ↘ EXECUTION_FAILED (terminal; recovery = rollback path)
RUNNING ↘ RECONCILIATION_REQUIRED (UNKNOWN_OUTCOME; recovery = observation only)
```

Recovery entry points per state (RecoveryManager, `mc612-recovery-v1`):

| Entry state | Recovery behavior |
|---|---|
| REQUESTED/PLANNED/CONFIRMATION_REQUIRED | pre-lease stale; no action |
| SNAPSHOT_CAPTURED | ready for execution |
| LEASED | validate lease; expired → not-started outcome |
| RUNNING | outcome marker missing → conservative UNKNOWN; reconciliation only |
| EXECUTED_PENDING_VERIFICATION | verification resumable (fresh read-back) |
| ROLLBACK_REQUESTED | rollback resumable |
| UNKNOWN_OUTCOME | reconciliation by observation; blind retry forbidden |
| terminal | no recovery action |

## Leases and fencing

One active lease per action (database-enforced). Fencing tokens are
monotonic per action and bound to the action version. Release matches on
`(action_id, lease_id, fencing_token, state='granted')` — a stale release can
never release a newer lease. Expired leases cannot commit; expired leases
before execution start may be superseded by a fresh grant (a new grant, not a
renewal — no silent renewal after execution has started).

## Capability registry

Typed `CapabilityDefinition`s declare: allowed environments, target type,
reversibility, snapshot/verification/reconciliation contracts, risk class,
privilege class, enabled flag. Fail-closed rules: enabled capabilities must
declare all contracts; automatic rollback requires reversibility; permanently
forbidden capabilities cannot be enabled. Default posture:

| Capability | Enabled | Environments | Notes |
|---|---|---|---|
| APPLY_PROJECT_PLAN | yes | staging only | the proven vertical slice |
| DRY_RUN_APPLY_PROJECT_PLAN | yes | staging only | records, performs nothing |
| RESTART_ALLOWLISTED_SYSTEMD_UNIT | no | (none declared) | typed preparation only |
| UPDATE_GIT_REF | no | (none declared) | blockers documented |
| REBUILD_COMPOSE_STACK | no | (none declared) | blockers documented |
| DOCKER_MUTATION | no | (none declared) | blockers documented |
| ARBITRARY_SCRIPT | never | (none) | permanently forbidden |

Production is structurally absent from every environment list. There is no
global external-execution switch.

## Executor registry

`capability_id + version → executor` mappings are registered explicitly at
application construction. Unknown capability, missing executor, version
mismatch, duplicate registration, and registration of permanently-forbidden
capabilities are all denied. No import side effects; no dynamic selection
from user input.

## Execution contract

`mc612-execution-contract-v2`, frozen, with every binding: capability,
capability_version, action, action_version, plan, plan_revision,
plan_digest, target, environment, snapshot, decision, confirmation, policy
version, verification version, lease, fencing token, kill-switch epoch,
expiry. A canonical serialization (`mc612-contract-digest-v1`) yields an
integrity digest over all fields; any field change changes the digest.

## Target binding

`TargetRecord`s are resolved from the durable plan store (target_id, plan
digest prefix, environment, typed allowed capabilities). Providers receive
resolved targets; they never reinterpret strings or select targets/environments.

## Audit

One canonical hash-chained ledger (`mc612-audit-v1`). Every security-relevant
operation (authorization, confirmation, snapshot, lease, execution,
verification, reconciliation, rollback) emits evidence atomically with its
state transition. Legacy update-plane JSON audit files are non-authoritative
execution detail.

## External execution boundary

External mutation is **disabled**. Enabling any external capability in the
future requires, per capability: an explicit registry entry with all safety
contracts defined, a registered executor, environment = staging only, and a
separately authorized deployment gate. Production is denied at five
independent boundaries: capability policy, authorization, contract, executor,
and (future) provider.

## Configuration

Startup validation fails closed on: missing owner verifier, malformed
capability registry, unsafe database permissions, zero staging targets, or
unsafe bind addresses. No silent defaults for security-critical configuration.

## Legacy update plane

`UpdateEngine` remains CLI-only and is **not** safely subordinate (no
digest-bound contract, unbounded `start_services.py` execution, no
rollback/reconciliation). Prerequisites for subordination are recorded in the
Shot-8 report. The bridge is intent-only (Stage A/B/C); dry-run is enabled;
real providers are unreachable from the control plane (source-enforced).

## Contract Evidence

Every executable action carries a durably persisted contract digest
(`mc612-contract-digest-v1`). The digest is a SHA-256 over the canonical
serialization of the full ExecutionContract (all security-relevant fields:
action, capability, plan, target, environment, confirmation, decision,
snapshot, lease, fencing token, kill-switch epoch, policy version,
verification version, expiry). The digest is bound at the first execution
attempt (LEASED state), is immutable (a different digest for the same action
is a conflict), and is idempotent on replay (same digest = no-op).

The executor verifies the stored digest against the contract digest before
mutation (via the FinalExecutionGate). Recovery and reconciliation read the
stored digest but never repair it. The `execution_started` audit event
carries the digest prefix in its result_code.

## Final Execution Gate

`FinalExecutionGate` (`mc612-execution-gate-v1`) is the single authoritative
pre-mutation check. It re-reads current world state: action version/terminal
state, contract digest binding, capability registry (enabled/version/env),
policy version, confirmation (exists/bound/unexpired/unconsumed), snapshot
(revision/target), plan revision+digest (TOCTOU), lease (active/fence/expiry),
kill switch (state/epoch), and contract expiry. Every check failure produces
a typed `ExecutionGateDecision(allowed=False, reason=GateCode.*)`. No external
mutation occurs unless the gate allows. The gate is called by the executor;
it is not duplicated in transport, provider, dashboard, or UpdateEngine.

## Security Invariants

1. Unauthorized actor → authorization denies (never reaches executor)
2. Expired confirmation → gate denies (CONFIRMATION_EXPIRED)
3. Reused confirmation → confirmation store denies (single-use)
4. Stale contract → gate denies (CONTRACT_DIGEST_MISMATCH)
5. Stale fence → lease/gate denies (LEASE_FENCE_MISMATCH)
6. Changed plan → gate denies (STALE_PLAN, TOCTOU re-read)
7. Changed target → gate denies (TARGET_MISMATCH)
8. Changed environment → registry denies (ENVIRONMENT_DENIED)
9. Changed policy → gate denies (POLICY_MISMATCH)
10. Changed kill-switch epoch → gate denies (KILL_SWITCH_EPOCH_MISMATCH)
11. Disabled capability → registry denies (CAPABILITY_DISABLED)
12. Unknown capability → registry denies (CAPABILITY_UNKNOWN)
13. Missing executor → registry denies (No executor registered)
14. Production → denied at capability registry + authorization + contract + gate
15. Unknown environment → denied (not defaulted to staging)

## CSRF Storage

Durable sessions store the CSRF token as `csrf_token_hash` = SHA-256(raw).
The raw token exists transiently inside `DurableSessionStore.create()` and in
the client flow; the database stores only the hash (which is itself opaque
and used as the client-facing token). Comparison is constant-time
(`secrets.compare_digest`). CSRF tokens rotate with session rotation and
epoch rotation; old tokens fail after rotation. CSRF values are never logged,
audited, or included in error responses.

## Execution Authority Boundary

```
transport (adapter, no authority)
  → ControlPlaneService (authority: authz, confirmation, lifecycle, audit)
    → FinalExecutionGate (authoritative pre-mutation check)
    → Executor (mechanism: mutation + verification)
      → CapabilityRegistry (typed posture, fail-closed)
      → ExecutorRegistry (explicit mapping)
      → TargetRecord (resolved binding)
```

The execution plane (executor, recovery, capabilities_registry) never
imports authentication, sessions, transport, Mission Control, or the
dashboard (source-scan enforced). Providers receive only ExecutionContract +
ExecutionGateDecision + TargetRecord.

## Provider Registration Rules

Executors are registered explicitly at application construction:
`executor_registry.register(capability_id=..., executor=...)`. No import
side effects, no plugin scanning, no environment-variable executor paths.
Registration refuses: duplicate registrations, permanently-forbidden
capabilities, unknown capabilities, and version mismatches. Provider
self-description is descriptive only — the registry is authoritative.

## Production Denial

Production is denied at five independent boundaries:
1. Capability registry: `production` is absent from every environment list
2. Authorization policy: production scopes are not in the allow-list
3. Action request: `ActionRequest.__post_init__` accepts production but the
   policy denies it
4. Execution contract: contract carries the environment from the action scope
5. Final execution gate: `require_executable` refuses production for every capability

There is no global bypass. No configuration option enables production.

## Lease Lifecycle

Leases follow a strict lifecycle: `granted → released` or `granted → expired`.
Release matches on `(action_id, lease_id, fencing_token, state='granted')` —
a stale release can never release a newer lease. On terminal outcome
(`VERIFIED_SUCCESS`, `ROLLBACK_SUCCEEDED`, `ROLLBACK_FAILED`) the lease is
durable-released. On expiry, the RecoveryManager advances the action to
`RECONCILIATION_REQUIRED`.

## Lease Expiry

An expired lease cannot commit. The RecoveryManager atomically advances
`LEASED + expired → RECONCILIATION_REQUIRED` via CAS. The action cannot
silently resume execution: the lease is gone, the gate denies, and the
recovery manager has already transitioned the lifecycle state.

## Recovery State Machine

```
LEASED + expired → RECONCILIATION_REQUIRED (CAS, atomic)
RUNNING + outcome_marker_missing → UNKNOWN_OUTCOME (conservative)
RUNNING + UNKNOWN_OUTCOME → reconciliation only
EXECUTED_PENDING_VERIFICATION → verification (fresh read-back)
RECONCILIATION_REQUIRED → reconciliation only; no blind retry
terminal → no recovery action
```

Recovery is idempotent (first call transitions, subsequent calls report),
concurrency-safe (CAS on version, single winner), and atomically audited.

## Rollback Gate

The rollback executor calls `FinalExecutionGate.evaluate()` on the rollback
action's own contract before any mutation. The rollback contract has its own
digest, independently bound and verified. The rollback gate validates:
confirmation, snapshot, capability, kill switch, lease, plan state, and
expiry — the same boundary as the original mutation.

## Unknown Outcome Recovery

UNKNOWN_OUTCOME is recoverable only through observation-based
reconciliation: fresh read of the plan state compared against the expected
post-condition. Post-state ⇒ mutation_succeeded; pre-state ⇒
mutation_not_started (retry is a policy decision); neither ⇒ stays UNKNOWN.
Blind retry is structurally forbidden.

## Final Mutation Fence

The actual mutation repository method enforces: action version (CAS),
expected plan revision (CAS), and the gate's kill-switch/lease/expiry
checks. The FinalExecutionGate is necessary but not sufficient — the
mutation boundary re-validates current state at the last possible moment
before the write.

## Crash Recovery Matrix

| Durable State | Crash/Expiry | Recovery |
|---|---|---|
| CONFIRMED | process dies | remain safe; execution can start |
| SNAPSHOT_CAPTURED | process dies | ready for execution |
| LEASED | lease expires | RECONCILIATION_REQUIRED (CAS) |
| RUNNING | process dies | UNKNOWN_OUTCOME → reconcile |
| EXECUTED_PENDING_VERIFICATION | process dies | verification (fresh read) |
| UNKNOWN_OUTCOME | restart | reconciliation only |
| RECONCILIATION_REQUIRED | restart | continue reconciliation |
| VERIFIED_SUCCESS | restart | terminal; no action |
| ROLLBACK_REQUESTED | restart | rollback path |
| ROLLED_BACK | restart | terminal |
| ROLLBACK_FAILED | restart | terminal; manual recovery |

## Known limitations

- Session store durability does not yet span a multi-instance deployment.
- The update-plane engine predates the control plane and remains operator-CLI territory.
- Rollback exists for the plan mutation only; external capabilities are
  non-reversible until their contracts are defined.
