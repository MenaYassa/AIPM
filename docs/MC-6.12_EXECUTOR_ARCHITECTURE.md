# MC-6.12 Executor Architecture

## Trust boundaries

```
CONTROL PLANE (authority)                 EXECUTOR SERVICE (mechanism)
├── AuthorizationPolicy                   ├── Structural request validation
├── OwnerConfirmationService              ├── Contract digest verification
├── FinalExecutionGate                    ├── SystemdRestartProvider
├── KillSwitchRegistry                    ├── Independent verification
├── AuditLedger                           └── Outcome classification
└── RecoveryManager
```

The control plane decides **WHETHER** an action is authorized.
The executor service decides **HOW** to perform the exact authorized mutation.
The executor does NOT perform business authorization.

## IPC

Unix domain socket at `/run/aipm/executor.sock`.
Length-prefixed JSON protocol (`mc612-executor-ipc-v1`).
Peer credential authentication (SO_PEERCRED).
Bounded request (4096 bytes) and response.
No shell, no arbitrary command strings.
Refused (unauthorized) callers have their pending request frame drained
(bounded, short timeout) before the refusal frame is sent, so clients
that write-then-read never hit EPIPE instead of the refusal.

## Mutation receipt: claim model and guarantees

The executor's mutation receipt store (`executor_mutation_receipts`,
UNIQUE (action_id, fencing_token)) is the durable replay fence. Precise
semantics (binding for all reports and claims):

- **Exactly-once durable claim.** At most one receipt can ever exist for
  a given (action_id, fencing_token). Enforced by the UNIQUE constraint
  plus a `BEGIN IMMEDIATE` claim transaction: the existence check and the
  INSERT run inside SQLite's serialized write lock, so concurrent claims
  for the same identity produce exactly one winner; every loser raises
  `MutationReceiptError("Mutation already claimed: ...")` (the stored
  status is included). The store uses per-call connections (WAL,
  busy_timeout=5000); no SQLite connection is ever shared between
  threads, which would corrupt Python-level statement state.
- **Single provider invocation under concurrent duplicate requests.**
  The provider (systemctl restart) is invoked only after a successful
  claim, so concurrent duplicate envelopes yield at most one external
  invocation; every other caller is fenced by the claim.
- **NOT exactly-once external side effect.** If the executor process dies
  between claim and the provider completing, the external effect may or
  may not have occurred. A receipt left in RECEIPT_CREATED therefore
  means "mutation attempted, outcome unknown" — it is NEVER automatically
  retried and the same (action_id, fencing_token) can never be re-claimed
  (a stale claim is permanent; recovery re-issues a NEW fencing token,
  which is a new identity, and reconciles by independent observation
  only).
- **Pre-provider failures do not linger.** Any executor-side failure
  between claim and provider invocation (unit resolution, pre-mutation
  observation) records `MUTATION_FAILED` with
  `provider_code="executor_error:pre_provider"` before re-raising, so a
  lingering RECEIPT_CREATED can only be a true mid-flight crash window.

These properties are certified by `tests/test_mc612_stage24c_exactly_once.py`
(concurrent duplicate identities at 20/50/100 workers, in-flight-claim
fencing, post-completion duplicates, and a 100-trial 20-worker stress).

## Identities

| Identity | Role | Sudo | NNP | Groups | Filesystem write access |
|---|---|---|---|---|---|
| `mina` | Human administrator | Broad (password) | n/a | sudo, docker | everything (admin) |
| `aipm` | Control plane | NONE | true | aipm, aipm-runtime | `/var/lib/aipm` only |
| `aipm-executor` | Executor service | Narrow NOPASSWD systemctl restart | absent | aipm-executor, aipm-runtime | `/var/lib/aipm-executor` only |

Group rules enforced by `ops/setup-aipm-identity.sh`:
- `aipm-executor` NEVER joins the `aipm` group — it has no access to control-plane state or DBs.
- `aipm-runtime` (read/execute on app code, dirs 0750 / files 0640, owner unchanged) contains exactly `aipm` and `aipm-executor` — no other members, verified at setup and re-verified on every run.

## Decision: Option B (separate executor service) selected

| Criterion | Shared identity (A) | Separate executor (B) | Root helper (C) |
|---|---|---|---|
| Blast-radius isolation | LOW (all same uid) | **HIGH** (dedicated uid + NNP for others) | HIGH |
| Complexity | Low | Medium | High |
| Auditability | Shared | **Per-service** | Per-invocation |
| Recovery | Complex (self-restart) | **Clean** (executor survives) | N/A |
| Self-restart | Yes (kills own process) | **No** (executor is separate) | No |
| Future capabilities | Broadens shared identity | **Scoped to executor** | Scoped to helper |
| Attack surface | All services share capability | **Only executor has capability** | Helper attack surface |
| Operational burden | Low | Medium | High |
| **Recommended** | | **✓** | |
