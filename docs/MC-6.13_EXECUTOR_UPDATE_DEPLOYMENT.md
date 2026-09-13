# MC-6.13 Executor Update Deployment Contract (C6.6-B/C6.6-C)

STATUS: **TEMPLATE + RUNBOOK — NOTHING IN THIS FILE IS INSTALLED OR ACTIVE.**

The executor base unit (`ops/systemd/aipm-executor.service`) ships
capability-OFF and network-isolated. The engine-backed update capability
(`--enable-update-plan`) must NEVER be enabled in the base unit. This
document is the versioned contract for the future, separately authorized
host deployment that enables it (C6.6-B design: GO). No command in this
file has been run against the host as part of this stage.

Ground rules baked into the contract:

- The update drop-in is the ONLY place where Docker authority, writable
  project roots, outbound network families, or `--enable-update-plan` may
  ever appear. Removing the drop-in always restores the fail-closed base.
- The IPC caller allow-list is exactly the numeric UID of the canonical
  control-plane principal (`aipm`, uid 997) — the principal that runs the
  operator transport and the dashboard. Numeric only: SO_PEERCRED delivers
  numeric UIDs and the executor CLI parses integers.
- No secret, token, or credential material belongs in any template here.

## 1. Canonical path contract

| Artifact | Canonical path | Provided by |
|---|---|---|
| Mutation receipt DB | `/var/lib/aipm-executor/state/receipts.db` | CLI default `--receipt-db` |
| Engine audit JSON | `/var/lib/aipm-executor/state/audit` | CLI `--update-audit-dir` (also the CLI default `<receipt-db dir>/audit`; startup probes writability and refuses to start otherwise) |
| Update backups (tar.gz snapshots) | `/var/lib/aipm-executor/state/backups` | `Environment=AIPM_BACKUP_DIR=…` |
| Executor log | `/var/lib/aipm-executor/logs/executor.log` | `Environment=AIPM_LOG_FILE=…` (REQUIRED: the config default `$HOME/.local/state/aipm/logs/…` sits outside `ReadWritePaths` and would fail engine composition at startup) |
| Temporary data | per-unit private `/tmp` | `PrivateTmp=true` |
| Executor config | `/var/lib/aipm-executor/.config/aipm/config.yaml` | Pre-created by the operator (aipm-executor:aipm-executor 0640); NO `AIPM_CONFIG` override — canonical `$HOME/.config/aipm` fallback (P2 decision) |

`BackupEngine` honors `AIPM_BACKUP_DIR`, `AuditService` honors
`AIPM_AUDIT_DIR` (the CLI passes the explicit audit dir), and
`MutationReceiptStore` takes the receipt DB path — all three operate with
the paths above; everything lives under the base unit's
`ReadWritePaths=/var/lib/aipm-executor/state /var/lib/aipm-executor/logs`.

## 2. Base unit contract (must hold forever)

MUST contain: `--allowed-caller-uids 997`; `SupplementaryGroups=aipm-runtime`
only; `ReadWritePaths=` limited to `/var/lib/aipm-executor/state` and
`/var/lib/aipm-executor/logs`; `RestrictAddressFamilies=AF_UNIX`;
`ProtectSystem=strict`; `ProtectHome=read-only`; `PrivateTmp=true`;
`RestrictSUIDSGID=true`; `RestrictNamespaces=true`; `LockPersonality=true`;
`ProtectKernelTunables/Modules/ControlGroups=true`; `UMask=0077`;
`User=aipm-executor`.

MUST NOT contain: `--enable-update-plan`; the `docker` group; any
`/home/ubuntu/...` path in `ReadWritePaths`; `AF_INET`/`AF_INET6`.

## 3. Update drop-in — TEMPLATE (do not install without explicit authorization)

To be created as
`/etc/systemd/system/aipm-executor.service.d/update-plan.conf` only after
every precondition in §7 passes. It is NOT checked in as a unit file
precisely so that it can never be mistaken for an installed, active unit.

```ini
# TEMPLATE — C6.6-B least-privilege update drop-in. Capability ON only when
# explicitly authorized. Removing this file + `systemctl daemon-reload` +
# `systemctl restart aipm-executor.service` restores the fail-closed base.
[Service]
# Group-accessible writes so mina-owned project trees never drift into
# single-user ownership (setgid dirs + core.sharedRepository do the rest).
UMask=0002
# docker = required by ComposeProvider (`docker compose up --build`) and the
# health analyzers. Root-equivalent authority: accepted explicitly per C6.6-B §2.
SupplementaryGroups=aipm-runtime docker
ReadWritePaths=/var/lib/aipm-executor/state /var/lib/aipm-executor/logs
# Enumerated registered project roots ONLY — never /home/ubuntu or /home/ubuntu/*.
# NOTE (systemd 255, verified): path-namespace options such as ReadWritePaths
# ACCUMULATE across assignments and across main+drop-in files; an empty-string
# assignment resets the list. Both lines below therefore APPLY (the second
# adds the project roots to the base state/logs carve-out).
ReadWritePaths=/home/ubuntu/aipm /home/ubuntu/invoicing /home/ubuntu/local-ai-packaged /home/ubuntu/EAG
# NO AIPM_CONFIG — the canonical executor config is
# /var/lib/aipm-executor/.config/aipm/config.yaml, pre-created by the
# operator (0640 aipm-executor:aipm-executor). It is only ever READ:
# under ProtectSystem=strict reads never need ReadWritePaths, and
# ConfigManager writes a default ONLY when the file is missing — a
# pre-created file makes startup write-free. (P2 decision; the former
# Environment=AIPM_CONFIG=/etc/aipm/executor/config.yaml line is removed:
# uid 995 cannot traverse /etc/aipm, so that path fail-closed at startup
# — resolved by elimination, not by any /etc/aipm permission change.)
Environment=AIPM_BACKUP_DIR=/var/lib/aipm-executor/state/backups
Environment=AIPM_LOG_FILE=/var/lib/aipm-executor/logs/executor.log
# AF_UNIX: executor IPC socket + Docker unix socket + journald.
# AF_INET/AF_INET6: Git fetch/pull over https + resolver ONLY. No TCP Docker API.
# Drill phase keeps AF_UNIX ONLY (file:// origin needs no AF_INET); the
# production drop-in later ADDS AF_INET AF_INET6 for GitHub HTTPS.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
# ExecStart= is a LIST setting: a drop-in APPENDS to the base entry unless
# the list is first cleared with an empty `ExecStart=` line (systemd 255
# rejects two entries for non-oneshot services). The FULL command (base
# flags + the appended update-plane flags) must then be restated as the
# single new entry. Empty-reset + complete restatement is mandatory.
ExecStart=
ExecStart=/home/ubuntu/aipm/.venv/bin/aipm executor run --allowed-caller-uids 997 --enable-update-plan --update-audit-dir /var/lib/aipm-executor/state/audit
```

Prerequisites before the drop-in may be installed: base unit corrected and
verified stable; executor config present (§4); per-project preparation (§5);
backup/audit dirs created with correct ownership; operator decision recorded
accepting the Docker authority analysis; operator transport
(`ops/systemd/aipm-operator-transport.service`, runs as `aipm`) deployed —
it is the canonical uid-997 IPC caller and is currently NOT installed.

## 4. Executor config — TEMPLATE (pre-create on host only during deployment)

Canonical location (P2 decision):
`/var/lib/aipm-executor/.config/aipm/config.yaml` — the executor's
`$HOME/.config/aipm` fallback (passwd home of uid 995 is
`/var/lib/aipm-executor`), owner `aipm-executor:aipm-executor`, mode
`0640` (parent `.config` dir `aipm-executor:aipm-runtime 0750`). The
operator PRE-CREATES the file once and the executor only ever reads it:
under `ProtectSystem=strict` reads never need `ReadWritePaths`, and
`ConfigManager` writes a self-healing default ONLY when the file is
missing — a pre-created file makes startup write-free. NO `AIPM_CONFIG`
override is set anywhere. The former `/etc/aipm/executor/config.yaml`
artifact (documented-but-never-consumed: no live process sets
`AIPM_CONFIG` to that path, and uid 995 cannot traverse
`/etc/aipm 750 root:aipm-provenance` — the template's old
`Environment=AIPM_CONFIG` line would have failed startup) must be REMOVED
during deployment (D4-B); do NOT chmod `/etc/aipm` and do NOT add o+x —
F-D3-3 is resolved by ELIMINATION of the dead path, never by widening.

```yaml
# TEMPLATE — /var/lib/aipm-executor/.config/aipm/config.yaml
# (pre-created by operator: aipm-executor:aipm-executor 0640)
#
# DRILL phase content: search_paths lists the drill root ONLY, so the
# four production roots are undiscoverable by the executor during the
# drill. The control-plane project_plans allow-list is a separate,
# independent mechanism (composition refuses to start on an empty or
# non-matching registered set) — with both planes scoped to the drill
# root, accidental execution against production targets is impossible
# by construction.
discovery:
  search_paths:
    - /home/ubuntu/aipm-drill
```

Production phase (later, separate authorization): replace the file
content so `search_paths` enumerates exactly the four production roots
(`/home/ubuntu/aipm`, `/home/ubuntu/invoicing`,
`/home/ubuntu/local-ai-packaged`, `/home/ubuntu/EAG`) and register those
targets in the control plane accordingly. Never copy the operator's own
`~/.config/aipm/config.yaml` wholesale.

## 5. Per-project Git sharing + registration runbook (uid 995)

For EACH registered project root (example shown for one root; repeat per
root). Record current ownership/modes before changing anything.

```bash
# Record (rollback input)
stat -c '%U:%G %a %n' <root> ; find <root> -maxdepth 2 -printf '%m %u:%g %p\n' > <root>.perms.before

# Group model: aipm-runtime already contains mina + aipm-executor.
chgrp -R aipm-runtime <root>
chmod g+rwX <root>
find <root> -type d -exec chmod g+s {} +          # new files inherit the group
# Shared Git object store (repo-local config IS valid for this key):
git -C <root> config core.sharedRepository group
find <root>/.git -type d -exec chmod g+rwX {} +   # one-time normalize

# Executor-side trust: explicit per-repo entry in the EXECUTOR's own config.
# Location: /var/lib/aipm-executor/.gitconfig (global config of uid 995).
#   [safe] directory = <root>
# safe.directory is honored ONLY from system/global config or -c — never
# repo-local. REJECTED: `safe.directory = *` (global wildcard) — it would
# make the executor trust every repository on the host. Enumeration keeps
# the trust set explicit, minimal, and auditable.
```

New project registration (static ReadWritePaths cannot support runtime
dynamic registration — registration is an operator procedure, restart-coupled):
1. add root to `/var/lib/aipm-executor/.config/aipm/config.yaml`
   search_paths (pre-created file; edit as operator);
2. add root to the drop-in `ReadWritePaths`;
3. apply §5 preparation to the root;
4. add the `safe.directory` entry to `/var/lib/aipm-executor/.gitconfig`;
5. `systemctl daemon-reload && systemctl restart aipm-executor.service`;
6. run the validation probes below.

Stale-project removal: reverse each step (remove root from both the
executor config and the drop-in, remove the gitconfig entry; leave
repository permissions as recorded in the rollback input), then reload +
restart. The dead `/etc/aipm/executor/config.yaml` artifact is never
referenced: remove it during deployment and keep `/etc/aipm` permissions
unchanged (no chmod, no o+x).

Validation (no mutation):

```bash
runuser -u aipm-executor -- git -C <root> status --porcelain    # no dubious-ownership error
runuser -u aipm-executor -- test -w <root>                      # write boundary holds
runuser -u aipm-executor -- test -w /var/lib/aipm               # must FAIL (CP DB denied)
runuser -u aipm-executor -- test -r /home/ubuntu/.git-credentials  # must FAIL
```

Rollback: restore recorded ownership/modes (`chown`/`chmod` from the
`.perms.before` capture), remove the executor gitconfig entry, remove the
root from config + drop-in, reload, restart. Removing the gitconfig entry
alone restores git's fail-closed dubious-ownership refusal.

## 6. Deployment sequence (summary; full step/abort/rollback table in C6.6-B report)

1. Commit + publish repository changes (unit file, this runbook).
2. Correct base unit `ExecStart` on host → `daemon-reload` → verify clean
   start, socket `0660 aipm-executor:aipm-executor`, uid≠997 refused,
   `execute_update_plan` → `capability_not_enabled`.
3. Create the executor config (§4, pre-created at
   `/var/lib/aipm-executor/.config/aipm/config.yaml` — drill-only
   search_paths) + state dirs (backup/audit) with correct ownership.
4. Verify CP-DB denial, project write boundary, backup/audit probes.
5. Per-project preparation (§5) + executor gitconfig.
6. Install drop-in (§3) → `daemon-reload` → restart.
7. Verify Docker reachability, address families, boundary probes again.
8. Disposable end-to-end update → disposable failure drill (rollback +
   receipt evidence) → staging execution → production enablement (separate
   authorization; production plan targets remain control-plane-gated).

Abort conditions (stop BEFORE any restart): missing/wrong allow-list flag;
uid ≠ 997 in allow-list; docker group in the BASE unit; unreadable audit
dir; writable CP DB; socket mode/owner wrong; project roots unregistered or
unwritable; git dubious-ownership on dry probes; unexpected sudo rules;
`/home/ubuntu`-wide grants anywhere.

## 7. Network contract

- `AF_UNIX`: executor IPC socket (`/run/aipm/executor.sock`, chmod 0660,
  SO_PEERCRED + exact UID allow-list), Docker unix socket
  (`/run/docker.sock` — image/registry traffic belongs to the Docker
  daemon, never the executor), journald.
- `AF_INET`/`AF_INET6`: Git fetch/pull over https and DNS resolution ONLY,
  and only inside the update drop-in.
- No TCP Docker API exists anywhere in the codebase (`docker.from_env()` is
  unix-socket only); none may be introduced.

## 8. Security acceptance checks (post-deployment)

All as `runuser -u aipm-executor -- …`: CP DB not writable/readable; mina
credentials unreadable; no write outside enumerated roots + own state; only
the drop-in grants docker (compare `id -nG aipm-executor` with and without
drop-in); no host systemd control from the update path; IPC caller
authentication remains exact uid 997; capability reports
`capability_not_enabled` when the drop-in is absent; receipts DB writable;
audit probe passes; failed disposable update leaves snapshot + audit JSON +
`MUTATION_FAILED`/UNKNOWN receipt (fail-safe evidence).
