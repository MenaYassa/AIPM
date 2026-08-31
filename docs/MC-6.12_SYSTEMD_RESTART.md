# MC-6.12 Systemd Restart Capability

## Status

**First real external capability.** Repository implementation complete; real execution requires privilege escalation not currently available to the operator identity.

## Capability

`RESTART_ALLOWLISTED_SYSTEMD_UNIT` — restarts exactly one pre-approved systemd unit via `systemctl restart <unit>`. Non-reversible; no automatic rollback.

## Selected unit

`aipm-telemetry.service` — the telemetry sampler is the safest candidate: pure observation, no persistent state, `Restart=on-failure` auto-recovers, no downstream dependencies.

## Allow-list

One entry: `SystemdRestartPolicy(environment="staging", target_id="aipm-telemetry", unit_id="aipm-telemetry", canonical_unit_name="aipm-telemetry.service")`. No wildcards, no globs, no regex. The executor resolves `unit_id → policy` before constructing the command.

## Privilege model

`systemctl restart aipm-telemetry.service` requires interactive authentication (polkit). The operator identity `mina` is in the `sudo` group but sudo requires a password; no NOPASSWD entry exists for systemctl. Real execution is blocked pending a privilege grant (e.g., a polkit rule or a sudoers entry for the specific command). No sudoers modification was made.

## Execution contract

The executor receives the canonical `ExecutionContract` (v2) + the resolved `SystemdRestartPolicy`. No raw HTTP fields, session data, or credentials reach the provider.

## Final Execution Gate

The `FinalExecutionGate` independently validates the rollback action's own contract: digest binding, confirmation, snapshot, capability, kill switch, lease, plan state, and expiry. Gate denial → no restart.

## Verification

After restart, the provider independently observes systemd (`systemctl show`) and verifies: unit exists, load_state=loaded, active_state=active, unit_id matches. PID change is recorded but not considered failure.

## Unknown outcome

Timeout or process loss → `UNKNOWN_OUTCOME`. Reconciliation is observation-only (never restarts). The action remains in `RECONCILIATION_REQUIRED` until an operator resolves it.

## Non-reversible semantics

`reversible = false`, `automatic_rollback = false`. A restart is an operational recovery action, not a reversible state mutation. If it fails, the operator performs manual recovery.

## Security

- `subprocess.run(argv, shell=False)` — structured argv, never shell
- Explicit executable path (`/usr/bin/systemctl`), resolved at construction
- Bounded stdout/stderr (4096 bytes), timeout (30s)
- No caller-controlled cwd, environment, or executable
- Source-scanned: no UpdateEngine, Git, Docker, or arbitrary command surface
