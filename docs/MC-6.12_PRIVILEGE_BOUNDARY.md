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
AIPM execution:       aipm (system user, no login, no password, no privileged groups)
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
mina ALL=(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service
```

This matches ONLY when argv is exactly `["/usr/bin/systemctl", "restart", "aipm-telemetry.service"]`.

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

```bash
echo 'mina ALL=(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service' | sudo tee /etc/sudoers.d/aipm-systemd-restart
sudo chmod 440 /etc/sudoers.d/aipm-systemd-restart
sudo visudo -c
```

## Broad sudo finding

`mina` has broad sudo (`(ALL : ALL) ALL` via the `%sudo` group in `/etc/sudoers`). This is the standard Ubuntu/Oracle Cloud VPS baseline and is required for VPS administration.

**This is NOT the AIPM execution privilege.** The distinction:

| Domain | Authority | Password | Usable by AIPM daemon |
|---|---|---|---|
| Human administrative (`mina`) | `(ALL : ALL) ALL` via `%sudo` group | Required | ✗ (no TTY, no password) |
| AIPM execution (`aipm`) | `(root) NOPASSWD: /usr/bin/systemctl restart aipm-telemetry.service` | Not required | ✓ |
| AIPM broad sudo (`aipm`) | N/A — aipm is NOT in the `%sudo` group | N/A | ✗ |

The AIPM daemon (running as `User=mina`) can invoke `sudo -n systemctl restart aipm-telemetry.service` (NOPASSWD) but CANNOT invoke `sudo -n whoami` or any other command (requires password, no TTY). This is verified by test.

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
requiring a password for each use. The AIPM daemon (running as `User=mina`
without a TTY or stored password) **cannot use this grant**. Only the
NOPASSWD systemctl restart rule is usable by the AIPM daemon. This is the
correct separation of concerns:

```
Human operator (mina)
  ├─ broad sudo (ALL:ALL) ALL     → password required → VPS administration
  └─ NOPASSWD systemctl restart   → AIPM execution capability

AIPM daemon (User=mina, no TTY)
  └─ NOPASSWD systemctl restart   → the ONLY sudo operation available
```

If the AIPM daemon is compromised, the attacker can only restart
`aipm-telemetry.service` — not execute arbitrary root commands.

## Removal

```bash
sudo rm /etc/sudoers.d/aipm-systemd-restart
```

## Security assumptions

1. AIPM runs as the dedicated `aipm` system user (NOT `mina`)
2. `aipm` is NOT in the `sudo`, `docker`, `admin`, or any privileged group
3. `aipm` has `/usr/sbin/nologin` shell — no interactive access
4. `aipm` has a locked password — no password login
5. `/usr/bin/systemctl` is root-owned and not writable by `aipm`
6. The unit file `/etc/systemd/system/aipm-telemetry.service` is root-owned and not writable by `aipm`
7. sudo's `env_reset` and `secure_path` defaults are active
8. No `SETENV` tag on the sudoers rule
9. The control plane remains the authorization authority; Linux privilege is the final defense
10. `sudo` timestamp caching is NOT a security boundary — the dedicated identity has no interactive sessions to create timestamps
11. Application code (`/home/ubuntu/aipm`) is read-only to the `aipm` identity
12. Runtime state (`/var/lib/aipm`) is owned by `aipm`
