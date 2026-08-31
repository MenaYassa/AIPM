# MC-6.12 Privilege Boundary

## Dedicated execution identity

The AIPM control plane runs as `aipm`. The executor service runs as `aipm-executor` — a separate Unix identity with the ONLY NOPASSWD systemctl privilege. Dashboard/events/telemetry have `NoNewPrivileges=true` and CANNOT invoke privileged execution.

**Key distinction:**
```
Human administrative identity (mina)  ≠  AIPM execution identity (aipm-executor)
Control plane identity (aipm)          ≠  Executor service identity (aipm-executor)
```

The control plane sends execution requests to the executor via Unix domain socket.
The executor validates independently and performs the mutation.
NoNewPrivileges=true is retained on ALL control-plane services.

```
Human administrator:  mina (uid=1003, groups: sudo, docker, aipm-provenance-client)
AIPM control plane:   aipm (system user, no login, no password, no privileged groups, no sudo)
AIPM executor:        aipm-executor (system user, no login, no password, no privileged groups;
                      ONLY sudo rule: NOPASSWD systemctl restart aipm-telemetry.service)
Shared read access:   group aipm-runtime (members exactly: aipm, aipm-executor) —
                      read/execute on application code; write access to nothing
```

Human administrative authority and AIPM machine authority are separate Unix principals.
`sudo` timestamp caching, TTY access, and shell environment from the human identity
are NOT available to the AIPM execution identity.

The dedicated `aipm` user is created by `ops/setup-aipm-identity.sh`.

## Threat model

The objective is NOT "make systemctl work". The objective is:

> grant AIPM exactly enough authority to restart one known service (`aipm-telemetry.service`) and nothing else.

Threats prevented:
- restart/stop/start/enable/disable of ANY other unit
- daemon-reload
- systemd property manipulation
- arbitrary command execution via sudo
- privilege escalation beyond restart
- changing unit files
- environment injection
- using the privilege boundary to execute shell
- using sudo to invoke other systemctl subcommands

## Mechanism comparison

| Mechanism | Authority | Unit restriction | Operation restriction | Attack surface | Complexity | Verdict |
|-----------|-----------|-----------------|----------------------|---------------|------------|---------|
| **polkit** | root (systemd) | ✗ any unit | ✗ any manage-units op | Medium (D-Bus) | Low | ✗ TOO BROAD |
| **sudoers** | root (exact argv) | ✓ exact args | ✓ exact args | Low (argv match) | Low | ✓ **SELECTED** |
| **helper** | root (setuid/service) | ✓ coded | ✓ coded | Medium (IPC) | High | Overkill for 1 op |

**Polkit rejected**: the `org.freedesktop.systemd1.manage-units` action cannot distinguish between restart/stop/start or between units. Granting it allows any unit operation.

**Helper rejected**: requires a setuid binary or D-Bus service with its own attack surface (IPC, argument parsing, authentication). Overkill for one operation.

**Sudoers selected**: exact command+argument matching (`/usr/bin/systemctl restart aipm-telemetry.service`). Sudo's `env_reset` clears dangerous environment variables. No `SETENV` tag. The `secure_path` default prevents PATH manipulation.

## Exact privilege

```
# /etc/sudoers.d/aipm-systemd-restart
aipm-executor ALL=(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service
```

This matches ONLY when argv is exactly `["/usr/bin/systemctl", "restart", "aipm-telemetry.service"]`, and is granted to the executor identity (`aipm-executor`), never to a human account.

### Bypass prevention

| Attack | Prevention |
|---|---|
| Extra args (`--now`, `--no-pager`) | sudoers requires exact arg match |
| Different verb (`stop`, `enable`, `daemon-reload`) | arg mismatch → denied |
| Different unit (`ssh.service`) | arg mismatch → denied |
| Alternate executable | sudoers specifies `/usr/bin/systemctl` (root-owned) |
| PATH manipulation | sudo `secure_path` default overrides PATH |
| Environment injection (`SYSTEMD_UNIT_PATH`) | sudo `env_reset` clears environment |
| `SETENV` abuse | `SETENV` tag not set |
| shell escape | sudo does not use shell |
| LD_PRELOAD | `env_reset` removes dangerous variables |
| Unit file modification | `/etc/systemd/system/aipm-telemetry.service` is root-owned 644, not writable by mina |
| Symlink to fake systemctl | sudoers specifies absolute path; `/usr/bin/systemctl` is root-owned 755 |

## Installation

Installed transactionally by `ops/setup-aipm-identity.sh --apply` (SUDOERS stage):

```bash
# candidate file is written to a mktemp location, chmod 440, then
candidate="$(mktemp)"
printf '%s\n' 'aipm-executor ALL=(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service' > "$candidate"
chmod 440 "$candidate"
sudo visudo -cf "$candidate"        # must pass BEFORE anything is installed
sudo install -m 440 "$candidate" /etc/sudoers.d/aipm-systemd-restart.new
sudo mv /etc/sudoers.d/aipm-systemd-restart.new /etc/sudoers.d/aipm-systemd-restart  # atomic rename
sudo visudo -c                      # post-install whole-file validation
```

Transaction properties:
- The prior rule (if any) is backed up to `/etc/sudoers.d/.aipm-backup/` (mode 0700) before replacement.
- On validator failure ONLY the candidate is removed; the existing rule is untouched.
- The install is an atomic `mv` on the same filesystem — no partial states.
- The legacy `mina`-based rule (`/etc/sudoers.d/aipm-systemd-restart-mina`), if present, is moved into the backup directory (preserved, not deleted).

Manual installation (only if the script cannot be used) must reproduce all of the above properties, then verify:

```bash
sudo visudo -c
```

## Broad sudo finding

`mina` has broad sudo (`(ALL : ALL) ALL` via the `%sudo` group in `/etc/sudoers`). This is the standard Ubuntu/Oracle Cloud VPS baseline and is required for VPS administration.

**This is NOT the AIPM execution privilege.** The distinction:

| Domain | Authority | Password | Usable by executor service |
|---|---|---|---|
| Human administrative (`mina`) | `(ALL : ALL) ALL` via `%sudo` group | Required | ✗ (separate identity) |
| AIPM execution (`aipm-executor`) | `(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service` | Not required | ✓ |
| AIPM broad sudo (`aipm-executor`) | N/A — aipm-executor is NOT in the `%sudo` group | N/A | ✗ |

The AIPM executor (running as `User=aipm-executor`) can invoke `sudo -n systemctl restart aipm-telemetry.service` (NOPASSWD) but CANNOT invoke `sudo -n whoami` or any other command. This is verified by test.

## Drift detection

**Two-layer verification model** (`mc612-privilege-check-v3`): (`mc612-privilege-check-v2`):

1. **Configuration evidence** (primary): the operator explicitly confirms
   the privilege boundary at application construction by setting
   `privilege_boundary_confirmed=True`. This is done after manual
   verification (using `sudo -l` or reading the file as root).

2. **Best-effort effective privilege check** (secondary): the application
   attempts `sudo -n -l` to read the effective privilege list. On this VPS,
   `sudo -n -l` requires authentication and will fail in non-interactive
   mode. When it fails, the application reports the limitation and relies
   on the operator's confirmation. When it succeeds (credential cache is
   active), the output is parsed for systemctl drift.

The application NEVER reads `/etc/sudoers` or `/etc/sudoers.d/` directly.
No filesystem permission weakening is required.

### Drift semantics

| Condition | Result |
|---|---|
| AIPM narrow rule present + no broader AIPM systemctl rules | ENABLED |
| Human broad sudo present (VPS baseline) + AIPM rule present | ENABLED |
| AIPM narrow rule missing | DISABLED (fail closed) |
| Broader AIPM systemctl NOPASSWD rule detected | DISABLED (drift, fail closed) |
| Operator not confirmed | DISABLED (fail closed) |

### Limitations

Without a root-owned verification mechanism, the application cannot
independently verify the effective privilege boundary. The operator must
manually verify using `sudo -l` and confirm at construction time. This is
an acceptable limitation for a single-owner system.

### Human vs AIPM privilege distinction

The broad sudo grant `(ALL : ALL) ALL` is **human administrative privilege**
requiring a password for each use. The executor service (running as
`User=aipm-executor`, no TTY, no stored password) **cannot use this grant**.
Only the NOPASSWD systemctl restart rule is usable by the executor identity.
This is the correct separation of concerns:

```
Human operator (mina)
  ├─ broad sudo (ALL:ALL) ALL     → password required → VPS administration
  └─ (no NOPASSWD AIPM rule)      → legacy mina rule migrated to backup

AIPM control plane (User=aipm)    → NO sudo rules at all

AIPM executor (User=aipm-executor)
  └─ NOPASSWD systemctl restart   → the ONLY sudo operation available
```

If the executor is compromised, the attacker can only restart
`aipm-telemetry.service` — not execute arbitrary root commands. The control
plane (`aipm`) holds no sudo privilege; it reaches the executor only through
the IPC socket, and the executor validates independently.

## Removal

```bash
sudo rm /etc/sudoers.d/aipm-systemd-restart
sudo visudo -c
```

(Prefer restoring the prior rule from `/etc/sudoers.d/.aipm-backup/` when
rolling back a cutover rather than deleting outright.)

## Security assumptions

1. AIPM control plane runs as the dedicated `aipm` system user (NOT `mina`)
2. The executor runs as the dedicated `aipm-executor` system user (NOT `mina`, NOT `aipm`)
3. Neither `aipm` nor `aipm-executor` is in the `sudo`, `docker`, `admin`, or any privileged group
4. Both service identities have `/usr/sbin/nologin` shells and locked passwords
5. `/usr/bin/systemctl` is root-owned and not writable by the service identities
6. The unit file `/etc/systemd/system/aipm-telemetry.service` is root-owned and not writable by the service identities
7. sudo's `env_reset` and `secure_path` defaults are active
8. No `SETENV` tag on the sudoers rule
9. The control plane remains the authorization authority; Linux privilege is the final defense
10. `sudo` timestamp caching is NOT a security boundary — the dedicated identities have no interactive sessions to create timestamps
11. Application code (`/home/ubuntu/aipm`) is read-only to the service identities: group `aipm-runtime` (members: exactly `aipm`, `aipm-executor`), dirs 0750, files 0640, owner unchanged
12. Runtime state (`/var/lib/aipm`) is owned by `aipm`; executor state (`/var/lib/aipm-executor`) is owned by `aipm-executor` — the executor has no access to control-plane state and never joins the `aipm` group
