# MC-6.13 Canonical Production Privilege Broker Contract

STATUS: **IMPLEMENTATION CONTRACT & OPERATIONAL EVIDENCE — PROVEN IN PRODUCTION.**
The canonical privilege broker and bounded systemd update runtime were successfully installed, executed, and reconciled in production (Steps 8B and 9).

---

## 1. Problem Statement & Motivation

AIPM's update engine coordinates updates across registered projects. For systemd-supervised services (such as `aipm-dashboard.service`), applying an update requires transitioning the systemd unit through a controlled service restart and verifying its post-restart health.

The AIPM update execution path runs under the unprivileged service identity:
```
User: aipm-executor (uid 995)
Group: aipm-executor (gid 984)
SupplementaryGroups: aipm-runtime (gid 983)
```

Directly delegating systemd management authority to `aipm-executor` via broad sudo or unconstrained Polkit rules is dangerous:
- `systemctl` includes subcommands that grant root command execution or shell escape (`systemctl edit`, `systemctl run`).
- `systemctl` allows modifying system security posture (`systemctl mask`, `systemctl disable`, `systemctl daemon-reload`).
- An unconstrained permission would allow the executor to restart or terminate critical host services, or inadvertently restart itself (`aipm-executor.service`), aborting in-flight transactions and corrupting mutation receipts.

To preserve the principle of least privilege, AIPM implements a **narrowly-scoped, compiled privilege broker**:

```
[Unprivileged Executor] (uid 995)
         │
         │ invokes via sudo with exact argv
         ▼
[Sudo Boundary] (/etc/sudoers.d/aipm-systemd-restart)
         │ (requires exact binary path + exact arguments; denies everything else)
         ▼
[Privileged Broker] (/usr/local/libexec/aipm/aipm-systemd-restart, root:root 0755)
         │
         ├─ Verifies EUID == 0 (root)
         ├─ Verifies argc == 3 (strictly 2 options)
         ├─ Verifies --unit= matches compiled allowlist (aipm-dashboard.service)
         ├─ Verifies --verb= matches approved operation (try-restart)
         ├─ Verifies no prohibited characters (@, /, \, ;, |, &, $, `, newlines, etc.)
         ├─ Discards caller environment (cleans env to PATH=/usr/bin:/bin)
         ├─ Invokes /bin/systemctl directly via execve() (no shell interpretation)
         ▼
[/bin/systemctl try-restart aipm-dashboard.service]
```

---

## 2. Exact Production Authority

### Approved Unit Allowlist
The broker allows operation **ONLY** on explicitly authorized systemd units:
- `aipm-dashboard.service`

The broker strictly rejects:
- `aipm-executor.service` (MUST NOT be restartable via broker)
- `aipm-telemetry.service`
- `aipm-events.service`
- Any unrelated host or OS systemd unit
- Glob patterns (`*.service`), template units (`@.service`), aliases, path traversal, or dynamic units.

### Approved Verb Allowlist
The broker accepts **ONLY** the approved update action:
- `try-restart`

The broker strictly rejects:
- `start`
- `stop`
- `restart`
- `reload`
- `daemon-reload`
- `enable`
- `disable`
- `mask`
- `unmask`
- `edit`
- Any arbitrary or custom systemctl verb.

---

## 3. Defense-in-Depth Privilege Architecture

The broker enforces two independent security boundaries:

### Layer 1: Sudoers Exact-Argument Binding
The sudoers configuration specifies the exact executable and exact arguments:
```sudoers
# /etc/sudoers.d/aipm-systemd-restart
aipm-executor ALL=(root) NOPASSWD: /usr/local/libexec/aipm/aipm-systemd-restart --unit=aipm-dashboard.service --verb=try-restart
```

Properties:
- Exact user: `aipm-executor` ONLY.
- Exact binary: `/usr/local/libexec/aipm/aipm-systemd-restart` ONLY.
- Exact arguments: `--unit=aipm-dashboard.service --verb=try-restart` ONLY.
- No wildcards (`*`), no regex, no shell wrappers.
- Default sudo options (`env_reset`, `secure_path`) prevent environment or PATH hijacking.
- Sudo rejects any deviation (different unit, different verb, extra arguments, shell metacharacters) at the privilege boundary before the broker binary runs.

### Layer 2: Compiled Binary Verification
The broker binary (`ops/broker/aipm-systemd-restart.c`) is compiled as a hardened ELF executable:
- **No Interpreter / Shebang**: Compiled C executable eliminates python/bash startup hijacking, `PYTHONPATH` manipulation, or script injection.
- **Root EUID Enforcement**: Ensures effective root credentials before parsing.
- **Strict Argc**: Rejects anything other than `argc == 3`.
- **Compile-time Allowlists**: Unit names and verbs are matched against static string arrays.
- **Environment Sanitization**: Passes an explicit minimal environment (`PATH=/usr/bin:/bin`) to `execve`.
- **Direct Systemctl Execve**: Never invokes `/bin/sh` or `system()`. Shell metacharacters are treated as literal invalid characters and rejected.

---

## 4. Unprivileged Client Integration

The application layer interacts with the broker through `PrivilegeBrokerClient`:
(`src/aipm/services/update/privilege_broker.py`)

- **Role**: Construct canonical CLI arguments (`--unit=<unit>`, `--verb=<verb>`) and execute the broker.
- **Fail-Closed Guarantees**:
  - Validates unit format against `^[A-Za-z0-9_.:-]+\.service$` and character allowlists before invocation.
  - Enforces `allowed_units` (defaults to `frozenset({"aipm-dashboard.service"})`).
  - Enforces `_ALLOWED_VERBS` (strictly `frozenset({"try-restart"})`).
  - Maps execution states deterministically:
    - Missing broker binary: `returncode = 127`, `success = False`
    - Permission denied / non-executable: `returncode = 126`, `success = False`
    - Subprocess timeout: `returncode = 124`, `success = False`
    - Non-zero exit (broker rejection): `returncode != 0`, `success = False`

---

## 5. Production Installation Contract (Template)

**DO NOT EXECUTE WITHOUT EXPLICIT HOST-MUTATION AUTHORIZATION.**

When authorized, the production deployment sequence is:

1. **Compile the production broker binary**:
   ```bash
   cd /home/ubuntu/aipm/ops/broker
   make clean && make
   ```
2. **Install broker to canonical root path**:
   ```bash
   sudo mkdir -p /usr/local/libexec/aipm
   sudo chown root:root /usr/local/libexec/aipm
   sudo chmod 0755 /usr/local/libexec/aipm
   sudo install -o root -g root -m 0755 /home/ubuntu/aipm/ops/broker/aipm-systemd-restart /usr/local/libexec/aipm/aipm-systemd-restart
   make clean
   ```
3. **Install exact sudoers rule transactionally**:
   ```bash
   candidate="$(mktemp)"
   cat << 'EOF' > "$candidate"
   aipm-executor ALL=(root) NOPASSWD: /usr/local/libexec/aipm/aipm-systemd-restart --unit=aipm-dashboard.service --verb=try-restart
   EOF
   chmod 0440 "$candidate"
   sudo visudo -cf "$candidate"
   sudo install -o root -g root -m 0440 "$candidate" /etc/sudoers.d/aipm-systemd-restart
   rm -f "$candidate"
   sudo visudo -c
   ```
4. **Post-installation validation**:
   - Verify file ownership and permissions:
     - `/usr/local/libexec/aipm` -> `root:root 0755`
     - `/usr/local/libexec/aipm/aipm-systemd-restart` -> `root:root 0755` (non-setuid)
     - `/etc/sudoers.d/aipm-systemd-restart` -> `root:root 0440`
   - Run executor-identity negative checks (verifying `aipm-executor.service` and arbitrary commands fail closed).

---

## 6. Prohibited Actions & Self-Update Invariant

1. **No Executor Self-Restart**:
   `aipm-executor.service` MUST NOT be restarted by this broker. An in-flight update coordinator running in the executor would lose its connection, leading to orphan processes or incomplete state receipts.
2. **No Wildcards**:
   Wildcards in sudoers or unit parsing are strictly forbidden.
3. **No Dynamic Policy Files**:
   The broker does not read policy files from project directories or `/var/lib/aipm-executor`. The allowlist is immutable in the compiled helper.

---

## 7. Explicit Per-Project Systemd Authority

AIPM enforces **zero implicit authority**. Projects cannot be restarted or managed via systemd unless explicitly registered in the project configuration (`config/aipm.yaml`):

```yaml
projects:
  aipm:
    runtime_mode: systemd
    systemd:
      allowed_units:
        - aipm-dashboard.service
      health_probe_type: http
      health_probe_url: http://127.0.0.1:8787/healthz
      health_probe_timeout_seconds: 15
      health_probe_expected_status: 200
```

### Invariants:
1. **Explicit `runtime_mode`**: Must be set to `systemd`. Projects defaulting to `docker` or `custom` are blocked from systemd broker execution.
2. **Explicit `allowed_units` Allowlist**: The planner and update engine only consider units explicitly named in `allowed_units`. Any unit outside this list is immediately rejected before plan authorization.
3. **Mandatory Health Probe Contract**: Each systemd-managed project must declare an application health probe contract (`health_probe_type`, `health_probe_url`, `health_probe_expected_status`, `health_probe_timeout_seconds`).
4. **No Default Authority**: If a project omits `projects.<name>`, systemd operations are disabled.

---

## 8. Two-Layer Systemd Verification Semantics

A systemd update is never considered successful on a bare `systemctl try-restart` returncode alone. The `SystemdVerifier` applies a strict two-layer verification contract:

### Layer 1: Supervisor State Verification
The supervisor state is sampled from `systemd` via D-Bus / `systemctl show`:
- **Active & SubState**: Unit must be `ActiveState=active` and `SubState=running`. Units in `failed`, `activating`, `deactivating`, or `inactive` trigger failure.
- **InvocationID Transition**: The `InvocationID` post-restart must **differ** from the pre-restart baseline. This proves that systemd created a genuine new unit lifecycle execution and that the process was not left running untouched.
- **Bounded Restarts (`NRestarts`)**: The unit restart counter must not exceed the baseline + 1 (`NRestarts <= baseline_restarts + 1`). Rapid crash loops or restart storms fail verification.
- **Service PID Transition**: When applicable, `MainPID` transition is recorded.

### Layer 2: Application Health Probe Verification
Once supervisor invariants hold, the verifier probes application layer health:
- **HTTP Probe**: Issues `GET` to the declared health probe URL (e.g., `http://127.0.0.1:8787/healthz`).
- **HTTP Status Check**: Status must match `health_probe_expected_status` (HTTP 200).
- **Body & Timeout**: Response must be received within declared timeout (15s) and indicate healthy service state.

### Ambiguous Outcomes & Recovery
If the broker restart call or health verification encounters an ambiguous outcome (e.g. process timeout or connection drop):
- The engine marks the action state as `RECONCILIATION_REQUIRED`.
- Blind automatic retries are strictly forbidden to prevent compounding failures.
- If pre-execution snapshot exists and the restart fails closed, automatic rollback is triggered to restore project files.

---

## 9. Production Operational Evidence & Reconciliation (Steps 8B & 9)

On 2026-09-16, the canonical production update plan was executed and verified against `aipm-dashboard.service`:

### Execution Artifacts & Invariants:
- **Plan Digest**: `1ecb2fa218d084f64e56240e0243f0feaf061fc0b93700da7ffc41e8827cb70c` (exact binding across planner, control plane, and execution contract).
- **Action ID**: `324a1efa19ec334b691446a7df985348`
- **Mutation Receipt**: `df6efb8344645da16ccc20d670327bde` (`mutation_succeeded`, provider code `update_ok`)
- **Terminal Lifecycle State**: `VERIFIED_SUCCESS` (`verified_success`)
- **Pre-Update Safety Snapshot**: `/var/lib/aipm-executor/state/backups/aipm_20260916T074104351703Z.tar.gz` (1,164,299 bytes, owner `aipm-executor`, valid tar.gz)
- **Engine Audit Record**: `/var/lib/aipm-executor/state/audit/20260916T074104229313Z_aipm.json`
- **Audit Ledger Chain**: 25 events checked; cryptographic hash chain valid.

### Supervisor State Transitions:
- **`aipm-dashboard.service`**:
  - MainPID: `4101064` → `164554` (transitioned)
  - InvocationID: `06c12f1ed37947c7932de1245d0d5564` → `195a72d8c1ae4a8ebe97b8ebc91f6b4e` (transitioned)
  - ActiveState / SubState: `active` / `running`
  - NRestarts: `0`
  - Health Probe: `GET http://127.0.0.1:8787/healthz` → `HTTP 200 OK` (`{"status":"ok"}`)
- **`aipm-executor.service` (Isolated)**:
  - MainPID: `2166156` (strictly unchanged)
  - InvocationID: `a924fbbeef214e71a37fe2af559e46ab` (strictly unchanged)

### Boundary Checkpoints:
- **Broker Binary**: `/usr/local/libexec/aipm/aipm-systemd-restart` (`root:root 0755`, SHA256: `f768de87b372577cc556a45044c25e82517915782aba442e4889353362e13d58`)
- **Sudoers File**: `/etc/sudoers.d/aipm-systemd-restart` (`root:root 0440`, SHA256: `c680b32a94c82eac70dbbfad607d9bd79bdb144d01d6996e6176391362299a8b`)
- **Git HEAD**: `6ba853d27ce92e908b3c4dc35c24419af8452dcb` (ahead 0, behind 0)
- **Protected Fixture**: `config/aipm-drill-transport.yaml` preserved and untouched.

---

## 10. Release Bookkeeping & Repository Artifacts

The implementation components in this repository include:
1. **Broker Source & Build System**: `ops/broker/aipm-systemd-restart.c`, `ops/broker/Makefile`.
2. **Client Interface**: `src/aipm/services/update/privilege_broker.py` (`PrivilegeBrokerClient`, error handling, input validation).
3. **Verification**: `src/aipm/services/update/systemd_verifier.py` (`SystemdVerifier`, supervisor and HTTP probes).
4. **Integration Tests**: `tests/test_production_privilege_broker.py` (unit, integration, and security tests for compiled broker and client).
5. **Configuration**: `config/aipm.yaml` (`projects.aipm` systemd declaration).
