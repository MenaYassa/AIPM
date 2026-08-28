# AIPM

> **Current-state notice — 2026-08-28:** This document is retained as part of the AIPM documentation record. Its historical design or milestone narrative remains valid as historical context, but current completion, publication, deployment, and live-observation claims are superseded by [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md) and [`docs/LIVE_VPANEL_READONLY_FINDINGS.md`](docs/LIVE_VPANEL_READONLY_FINDINGS.md). The current tracked repository is synchronized at `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`; MC-6.12 operational execution remains blocked, and the incident-reopen workstream remains preserved separately in `stash@{0}`.


AIPM is a local **AI Platform Manager** for discovering and operating projects that use Git and Docker Compose. It provides a Typer-based command-line interface for host diagnostics, project discovery, Compose lifecycle operations, Docker inspection, Git safety checks, health reports, configuration snapshots, and guarded updates.

## Current status

The repository includes the core CLI, domain models, service/provider architecture, Git and Docker integrations, health analyzers, backup snapshots, a guarded update workflow, and the read-only Mission Control operations cockpit. The implementation is designed to remain useful on machines without a running Docker daemon: configuration, help, host diagnostics, and Git-only discovery do not require Docker. Docker- or Compose-specific commands return a clear error when the required runtime is unavailable.

The current Mission Control checkpoint is `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`, synchronized with `origin/main` and remote `main`. MC-6.1 through MC-6.8 are complete for the bounded read-only cockpit; MC-6.9 is PASS_EXISTING; MC-6.10 is complete under the safe null/not_observed posture contract; MC-6.11 is landed; MC-6.12 contains only non-executing foundations and its operational action plane remains blocked; and MC-6.13 is complete through Phase 4E for its bounded read-only advisor scope. The live vpanel is functioning but reports bounded stale/unavailable states where evidence is stale or unavailable. See [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md) for the reconciled ledger and [`docs/LIVE_VPANEL_READONLY_FINDINGS.md`](docs/LIVE_VPANEL_READONLY_FINDINGS.md) for fresh web evidence.

For the complete milestone ledger, remaining roadmap, safety invariants, deployment gates, and next steps, see [`docs/MC-6_STATUS.md`](docs/MC-6_STATUS.md). The broader AIPM update-management roadmap remains in [`PRODUCTION_ROADMAP.md`](PRODUCTION_ROADMAP.md), while the historical baseline and completion records remain in [`PROJECT_STATUS.md`](PROJECT_STATUS.md).

## Installation

AIPM requires Python 3.12 or newer. From a checkout, install the package and development dependencies with:

```bash
python -m pip install -e '.[dev]'
```

The package declares its runtime dependencies, including Typer, Rich, PyYAML, Docker SDK, psutil, HTTPX, and GitPython. Docker Compose operations additionally require the Docker CLI with the Compose v2 plugin, and Docker must be running for runtime commands.

## Quick start

```bash
aipm --help
aipm version
aipm doctor
aipm discover
```

AIPM creates a default configuration at `~/.config/aipm/config.yaml` on first use. The path can be overridden with `AIPM_CONFIG`:

```bash
AIPM_CONFIG=/path/to/config.yaml aipm discover
```

## Mission Control dashboard

AIPM includes a read-only web dashboard that turns the VPS handbook into a live operations cockpit. It reports host telemetry, Docker container state, local cloudflared visibility, and Git/Compose-backed project inventory while linking back to the handbook’s first-response workflows. The current public path is an existing bridge-bound ingress: Cloudflared container → `172.20.0.1:8788` → host nginx reverse proxy → `127.0.0.1:8787` → dashboard, serving `vpanel.03092017.xyz`. The dashboard itself remains loopback-bound. Separately supplied production checks confirmed the bridge-side `/healthz` and public `/healthz` plus `/api/overview`; these are live VPS results and not changes made by Phase 4A.

Start it locally with:

```bash
aipm dashboard
```

The default listener is `127.0.0.1:8787`; do not bind it to `0.0.0.0:8787`. The existing public path uses the host nginx bridge listener at `172.20.0.1:8788`, which forwards only to the loopback dashboard. The full deployment and security runbook is in [`docs/MISSION_CONTROL.md`](docs/MISSION_CONTROL.md), and the current milestone ledger is in [`docs/MC-6_STATUS.md`](docs/MC-6_STATUS.md). Mission Control uses typed read-only telemetry services behind the existing AIPM providers and preserves the current `/api/overview` response contract. MC-2 adds a configurable SQLite sampler through `aipm telemetry sample` and `aipm telemetry run`, plus safe `/api/history/host`, `/api/history/containers`, `/api/history/projects`, and `/api/history/tunnel` endpoints. MC-3 adds a separate deterministic processor through `aipm events process` and `aipm events run`, with `/api/events`, `/api/incidents`, and a focused read-only Incident Room. MC-6 adds the incremental cockpit shell, Server, Docker, Project/Application, Systemd, and bounded Logs pages. See [`docs/MISSION_CONTROL.md`](docs/MISSION_CONTROL.md) for the schema, retention, Systemd templates, idempotency, and safety boundaries.

## MC-6.13 AI Advisor domain

MC-6.13 Phases 2, 3, 4A, 4B, fixture/live 4C presentation, 4C.1, 4C.2, 4C.3, 4D, and 4E are complete and pushed. The live advisor uses one bounded server-owned GET assessment through the telemetry-owned export, Phase 4D adapter, and deterministic composition boundary. The response preserves aligned evaluation semantics and adds only the bounded `resource_history_summary`; it performs no LLM/provider work, autonomous action, remediation, or polling. Cloudflare Access remains the selected perimeter boundary when enabled, while AIPM does not verify edge JWTs or proxy identity headers. MC-6.12 operational execution remains separately blocked. See [`docs/MC-6.13_STATUS.md`](docs/MC-6.13_STATUS.md) and [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md).

## Commands

| Command | Purpose |
|---|---|
| `aipm version` | Display the installed AIPM version. |
| `aipm hello` | Run a simple CLI sanity check. |
| `aipm doctor` | Display host, CPU, memory, disk, Python, and operating-system telemetry. |
| `aipm discover` | Find Git- or Compose-backed projects under configured search paths. |
| `aipm health PROJECT` | Run Git, Compose, and Docker health analyzers for a project. |
| `aipm backup PROJECT` | Create a compressed safety snapshot of a project. |
| `aipm update PROJECT --dry-run` | Build and display a side-effect-free update plan and write a dry-run audit record. |
| `aipm update PROJECT --yes` | Approve and execute the planned update transaction, then verify health and write an audit record. |
| `aipm compose ps PROJECT` | List containers belonging to a Compose project. |
| `aipm compose up PROJECT` | Start a Compose project. |
| `aipm compose down PROJECT` | Stop and remove a Compose project. |
| `aipm docker ps` | List Docker containers. |
| `aipm docker inspect NAME` | Display a mapped or raw container inspection. |
| `aipm docker start NAME` | Start a container. |
| `aipm docker stop NAME` | Stop a container. |
| `aipm docker restart NAME` | Restart a container. |
| `aipm docker logs NAME --tail 100` | Display recent container logs. |
| `aipm docker images` | List local Docker images. |
| `aipm docker volumes` | List Docker volumes. |
| `aipm docker networks` | List Docker networks. |
| `aipm git pull PROJECT` | Pull a project only when its working tree is clean and an `origin` remote exists. |

## Configuration

The configuration file has `logging` and `discovery` sections. A minimal example is:

```yaml
logging:
  level: INFO
  file: ~/.local/state/aipm/logs/aipm.log
  max_size_mb: 10
  backup_count: 3

discovery:
  search_paths:
    - ~/projects
  ignore_dirs:
    - .git
    - .venv
    - node_modules
    - __pycache__
  max_depth: 4
  follow_symlinks: false
```

Discovery is read-only and does not fetch remotes. Git snapshots report local branch state, dirty files, conflicts, ahead/behind information when remote-tracking refs are already available, and basic commit metadata.

## Safety behavior

`aipm update PROJECT --dry-run` builds a plan without creating snapshots, fetching remotes, pulling changes, restarting services, or writing project state. A normal update displays the same plan and requires explicit `--yes` approval. Critical Git changes, unresolved conflicts, detached heads, and other review conditions block execution. Non-critical local changes may be preserved in a named safety stash during an approved update; if stash application conflicts, the stash is preserved and the transaction stops.

Approved updates create a compressed snapshot before running a custom `start_services.py` runtime or rebuilding a Compose stack. Snapshots are stored under `~/.local/state/aipm/backups` by default and can be redirected with `AIPM_BACKUP_DIR`. Update plans and outcomes are recorded as redacted JSON under `~/.local/state/aipm/audit`, configurable with `AIPM_AUDIT_DIR`.

The update workflow deliberately does not force-kill arbitrary host processes, run hard-coded privileged ownership changes, or silently overwrite local work. If deployment or post-update checks fail, the command reports the snapshot path so the operator can review and restore it using an intentional recovery procedure.

## Update planning

The update workflow is organized around a side-effect-free `UpdatePlanner`, a typed `UpdatePlan`, and an execution/audit boundary. Planning combines health-before analysis, Git state, runtime detection, risk classification, ordered actions, snapshot requirements, and approval requirements. The planner does not mutate the project. The executor performs state-changing operations only after `--yes`, preserves local safety stashes, verifies health afterward, and records `planned`, `blocked`, `approval_required`, `failed`, or `success` outcomes.

The detailed production gap analysis and roadmap are in [`PRODUCTION_ROADMAP.md`](PRODUCTION_ROADMAP.md).

## Architecture

The application is organized into layers:

| Layer | Responsibility |
|---|---|
| `src/aipm/cli` | Typer command routing. |
| `src/aipm/capabilities` | User-facing command presentation with Rich output. |
| `src/aipm/services` | Application workflows such as discovery, Compose status, backups, health, and updates. |
| `src/aipm/providers` | External-system adapters for Docker, Compose, and Git. |
| `src/aipm/engines` | Health analyzer orchestration and report construction. |
| `src/aipm/models` | Dataclasses and enums representing domain state. |
| `src/aipm/mappers` | Conversion from SDK objects into domain or presentation models. |

## Development and verification

Run the automated tests with:

```bash
pytest -q
```

The tests cover deterministic domain logic, configuration, discovery, backups, mapping, provider command construction, health aggregation, dry-run side-effect protection, approval gating, update execution, audit serialization, Mission Control contracts, read-only SQLite behavior, frontend safety, bounded log redaction, MC-6.13 evidence normalization, deterministic rule evaluation, Phase 4A composition, and the private authenticated Phase 4B advisor evaluation API. The final Phase 4B validation passed 17 focused tests and 516 full tests with the existing unrelated Starlette/httpx deprecation warning. Docker integration tests, disposable Git-remote tests, browser acceptance, and TUI tests remain separate roadmap work.

## License

See [`LICENSE`](LICENSE).


## MC-4 Alerts and Notifications

MC-4 adds incident-aware notification decisions, a persistent SQLite outbox, delivery attempt audit, and a dedicated `aipm notifications run` worker. Notifications remain disabled unless explicitly enabled in configuration. Channel credentials are referenced through environment-variable names and are never stored in Git, SQLite, logs, API responses, or browser payloads.

Useful commands are `aipm notifications list`, `aipm notifications retry NOTIFICATION_ID`, `aipm notifications test CHANNEL_ID`, and `aipm notifications run`. The dashboard exposes read-only notification, channel, and policy views at `/api/notifications`, `/api/notifications/{id}`, `/api/notification-channels`, and `/api/notification-policies`.

MC-4 does not perform remediation and does not modify Docker, Compose, Git, systemd, Cloudflare, packages, incidents, or infrastructure state. It stops before MC-5 Guarded Operations and MC-6 AI Advisor. See [`docs/MC-4_ARCHITECTURE.md`](docs/MC-4_ARCHITECTURE.md).


## MC-4.5 production hardening

MC-4.5 hardens the MC-4 notification subsystem with bounded channel retries, transactional incident and rate-window suppression, compare-and-set delivery claims, lease recovery, schema versioning and foreign keys, explicit UNKNOWN reconciliation, timestamp-based retention, operational metrics, and stricter startup validation. Additional commands are `aipm notifications reconcile`, `aipm notifications retain`, and `aipm notifications metrics`; the dashboard adds `GET /api/notification-metrics`.

The dashboard continues to bind to `127.0.0.1` by default. The documented public hostname is protected by the confirmed Cloudflare Access edge boundary; AIPM relies on that private edge protection and does not implement application-level JWT verification, identity middleware, or browser session handling. MC-4.5 does not modify Cloudflare, systemd, Docker, Compose, Git projects, packages, or VPS infrastructure. See [`docs/MC-4.5_PRODUCTION_RUNBOOK.md`](docs/MC-4.5_PRODUCTION_RUNBOOK.md).

## Current-state reconciliation — 2026-08-28

The canonical current-status record is [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md). The repository checkpoint is `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`, and local `HEAD`, `origin/main`, and remote `main` are equal with ahead/behind `0/0`, a clean worktree, and no staged files. The preservation stash `stash@{0}` remains intentionally untouched and contains the separate incident-reopen workstream plus `docs/MC-6.9_DESIGN.md`; those items are not part of published current main.

The read-only Mission Control cockpit is substantially landed and live. Fresh web inspection confirmed the dashboard, server, Docker, projects, bounded logs, incidents, history, settings posture, and read-only advisor surfaces. The advisor returned fresh aligned evidence with 18/18 coverage and six points spanning 300 seconds at 60-second cadence for CPU, memory, and disk. Live observations also show bounded stale/unavailable states, including stale MC-3 freshness, stale container resource observations, unavailable Systemd entries, and disabled/unavailable notification audit data. HTTP evidence does not establish the deployed Git commit, systemd unit contents, database ownership, producer convergence, or Cloudflare configuration; the live Settings surface reports `commit=Unknown`, `public_ingress=not_observed`, and `permanent_service=not_observed`.

MC-6.12 is foundation-only, not an operational action plane. No executor, action route/UI, durable operational state, leases/fencing, production target, service account, production authorization, autonomous remediation, LLM/provider execution, or notification delivery is enabled. Database merge/delete/repair/migration/rekey operations remain unauthorized.
