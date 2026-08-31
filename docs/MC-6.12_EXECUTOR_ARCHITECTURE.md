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
