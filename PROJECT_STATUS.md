# AIPM Project Status

> **Current-state notice — 2026-08-28:** This document is retained as part of the AIPM documentation record. Its historical design or milestone narrative remains valid as historical context, but current completion, publication, deployment, and live-observation claims are superseded by [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md) and [`docs/LIVE_VPANEL_READONLY_FINDINGS.md`](docs/LIVE_VPANEL_READONLY_FINDINGS.md). The current tracked repository is synchronized at `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`; MC-6.12 operational execution remains blocked, and the incident-reopen workstream remains preserved separately in `stash@{0}`.


**Historical assessment date:** 2026-08-16
**Historical repository reference:** [MenaYassa/AIPM][1]
**Historical assessed branch:** `main` at `97e3d06` (`Refactor health engines, models, and providers`)

## Historical baseline summary

AIPM is a Python command-line application intended to act as an **AI Platform Manager** for locally hosted projects. The repository already contains a recognizable capability/service/provider architecture, domain models for Docker, Compose, Git, host telemetry, backups, and health reports, together with a Typer CLI. The latest commits have moved the project toward a health-analyzer framework and transactional update workflow.

The project is **not yet runnable from a clean checkout** and has no automated test suite. The first executable smoke test failed during CLI import because two runtime dependencies used by tracked source files are absent from `pyproject.toml`: `GitPython` (`import git`) and `python-on-whales` (`from python_on_whales import DockerClient`). After installing only the declared dependencies, `aipm --help` still fails before command dispatch. Several flows also need defensive handling around missing Docker, absent Git remotes, configuration portability, and update safety.

This document records the baseline discovered before the completion pass. It is intentionally separate from the final implementation notes so that the original state and the work required to finish it remain auditable.

## What is already implemented

| Area | Current implementation | Evidence |
|---|---|---|
| Packaging and CLI entry point | A `setuptools` package named `aipm` is configured with an `aipm` console script and a `python -m aipm` entry point. | `pyproject.toml`, `src/aipm/__main__.py` |
| CLI command groups | Typer commands exist for version, hello, doctor, discover, health, backup, update, Docker operations, Compose operations, and Git operations. | `src/aipm/cli/app.py`, `src/aipm/cli/docker/app.py`, `src/aipm/cli/compose.py`, `src/aipm/cli/git.py` |
| Layered architecture | The source is divided into CLI capabilities, application/core services, providers, mappers, engines, and models. | `src/aipm/` package layout |
| Configuration | `ConfigManager` loads `~/.config/aipm/config.yaml` or creates a default configuration using YAML. | `src/aipm/core/config.py`, `src/aipm/models/config.py` |
| Logging and centralized application | `Application.create()` provides a singleton-style application object containing configuration, logging, Docker, and system services. | `src/aipm/core/app.py` |
| Docker provider | Docker container listing, inspection, start, stop, restart, logs, images, volumes, and networks are represented through the Docker SDK. | `src/aipm/providers/docker/provider.py`, `src/aipm/services/docker/service.py` |
| Docker presentation | Docker containers and system resources have mapper/view helpers and CLI capabilities for common read and lifecycle operations. | `src/aipm/mappers/docker.py`, `src/aipm/capabilities/docker/`, `src/aipm/cli/docker/app.py` |
| Project discovery | Configured search paths are scanned for Compose files and Git repositories; discovered projects are represented by a `Project` model. | `src/aipm/services/project/service.py`, `src/aipm/models/project.py` |
| Compose integration | Compose files are discovered, project-scoped Compose operations exist, and Compose services can be displayed. | `src/aipm/providers/compose/provider.py`, `src/aipm/capabilities/compose/management.py` |
| Git snapshot and actions | Git repository state includes branch, SHAs, dirty files, ahead/behind counts, stashes, remote metadata, and basic fetch/pull/stash/restore operations. | `src/aipm/providers/git/provider.py`, `src/aipm/models/git.py` |
| Health domain | Findings, severities, health states, health reports, recommendations, analyzers, and a report builder are defined. | `src/aipm/models/finding.py`, `src/aipm/models/health.py`, `src/aipm/models/health_report.py`, `src/aipm/engines/health/` |
| Health checks currently present | Git cleanliness, Compose service states, and Docker/Compose-related findings are partially analyzed. | `src/aipm/engines/health/analyzers/`, `src/aipm/services/health/service.py` |
| Safety snapshot | A project configuration snapshot is written as a compressed archive before updates. | `src/aipm/services/backup/engine.py`, `src/aipm/capabilities/backup/snapshots.py` |
| Transactional update intent | The update engine attempts to snapshot state, enforce Git cleanliness, rebuild Compose infrastructure, and run a health check. | `src/aipm/services/update/engine.py` |
| Host telemetry models | CPU, memory, disk, host, and system summary models/services are present. | `src/aipm/models/{cpu,memory,disk,host,system}.py`, `src/aipm/services/system/service.py` |

## What was incomplete at the baseline

### 1. Clean installation is broken

The declared dependency list contains Typer, Rich, PyYAML, Docker, HTTPX, and psutil, but tracked code imports `git` and `python_on_whales`. A clean installation therefore cannot import the CLI. This is a release-blocking packaging defect, not an optional enhancement.

### 2. There is no automated test suite

The repository contains no `tests/` files. The health score calculation, project discovery, configuration loading, Docker/Compose mapping, Git state handling, snapshot creation, CLI error behavior, and update guardrails are consequently unverified.

### 3. Provider initialization is too eager

`Application` creates `DockerService`, and `DockerService` creates a Docker provider during application startup. That means commands that only need configuration, discovery, Git, or help can fail on machines without a reachable Docker daemon. Providers should be lazy or degrade gracefully, while operations that genuinely need Docker should return a clear domain error.

### 4. Compose integration is fragile

The Compose provider depends on `python-on-whales` without declaring it. It also assumes a Docker daemon and mixes `python-on-whales` for lifecycle operations with the low-level Docker SDK for listing containers. The implementation needs consistent error translation, a fallback strategy or a clearly documented dependency, and testable boundaries.

### 5. Git state handling is incomplete

The Git provider performs a fetch while merely inspecting a repository, which makes discovery network-dependent and potentially slow. It also catches `Exception` broadly, silently converting unrelated failures into “not a Git repository.” Detached HEADs, repositories without `origin`, unborn branches, and remote-tracking refs need explicit handling. `changed_files()` does not include all categories of changes, such as untracked files, even though the domain model has a separate field for them.

### 6. Configuration is not portable enough

The default configuration is created under the user’s home directory, but the repository’s `config/aipm.yaml` is not used by default. Configuration serialization relies on dataclass `__dict__`, and there is no documented schema, validation, environment override, or safe handling of malformed values.

### 7. Health reporting is only partially integrated

The repository contains both a newer `HealthEngine`/`HealthReport` path and an older `HealthService` path. The CLI uses the engine, while the service retains a separate result model. Recommendations are defined but the report builder always returns an empty recommendation list. Analyzer errors are not isolated, and Docker availability can turn a diagnostic into an opaque exception rather than a useful finding.

### 8. Update behavior is not fully transactional

The update engine creates a snapshot and checks Git status, but it does not implement a complete rollback path after Compose or custom runtime failure. It also contains environment-specific behavior (`sudo chown` for a `searxng` directory and port cleanup) that should be optional, explicit, and testable. A failed deployment can therefore leave the project changed despite the “transactional” command name.

### 9. CLI consistency and error handling need completion

Some commands use the common `cli_handler` decorator while Docker lifecycle commands call services directly. Return values are inconsistent, errors are sometimes swallowed, and the CLI cannot reliably communicate a non-zero exit status for all failures. Help text and command options are also sparse for a tool intended to manage multiple projects.

### 10. Documentation and developer tooling are missing

The README is empty. There is no usage guide, configuration reference, architecture overview, development guide, test command, release checklist, or supported-environment statement. There is also no linting/type-checking/test configuration.

## Baseline verification performed

The repository was cloned from the selected GitHub repository and checked at the `main` branch. The declared dependencies were installed in editable mode so the package could be exercised. The following results were observed:

| Check | Result | Meaning |
|---|---|---|
| Repository status | Clean at the starting commit | No pre-existing local edits were present before this completion pass. |
| `aipm --help` after declared-dependency install | Failed | Import stopped at `ModuleNotFoundError: No module named 'python_on_whales'`. |
| Import of the application before dependency install | Failed | The environment initially lacked the declared runtime packages. |
| TODO/pass scan | Found intentional exception/analyzer stubs plus incomplete provider/backup areas | The scan is a lead list, not proof that every `pass` is a defect. |
| Automated tests | None found | Behavior was not protected by regression tests. |
| GitHub issue tracker | No open issues | The repository itself does not provide a prioritized completion backlog. |

## Completion plan

The completion pass should make the project installable and runnable without a Docker daemon, while preserving clear failures for Docker-specific commands. It should unify configuration and provider error handling, make project discovery deterministic and offline by default, complete health report aggregation and recommendations, strengthen backup/update safety, and add tests for the domain logic and provider boundaries. The README and this status document should then be updated with installation, usage, configuration, development, and verification instructions.

A truly production-grade release would still benefit from integration tests against disposable Git repositories and a Docker Compose fixture, but the application should not require privileged host changes or a live Docker daemon just to display help, inspect configuration, or run filesystem/Git-only operations.

## References

[1]: https://github.com/MenaYassa/AIPM "AIPM repository"
[2]: https://github.com/MenaYassa/AIPM/blob/main/pyproject.toml "AIPM package metadata and dependencies"
[3]: https://github.com/MenaYassa/AIPM/tree/main/src/aipm "AIPM application source tree"

## Completion pass delivered

The completion pass addressed the release-blocking baseline issues and added regression coverage. The package now declares GitPython, the undeclared `python-on-whales` import was removed in favor of a small Docker Compose CLI adapter, and `aipm --help`, `aipm version`, configuration bootstrap, host diagnostics, and Git-only discovery work from a clean editable install.

| Delivered change | Result |
|---|---|
| Dependency metadata | Added `GitPython` and a `dev` extra containing pytest. |
| Compose provider | Uses `docker compose` with project-scoped files and translates missing CLI, daemon, and command failures into `ComposeError`. |
| Docker provider | Translates client initialization, container, logs, image, volume, and network failures into domain errors; initialization is lazy at the application level. |
| Configuration | Added `AIPM_CONFIG`, validation, safe YAML serialization, and clearer first-run errors. |
| Discovery | Honors configured depth and ignore rules, sorts results deterministically, and treats discovered projects as scan boundaries. |
| Git | Discovery no longer fetches remotes implicitly; detached heads, missing remotes, conflicts, staged changes, and command failures are handled explicitly. |
| Health | Git, aggregate Compose, and per-container Docker analyzers now produce findings; report scores, states, and deduplicated recommendations are complete; analyzer failures are isolated. |
| Backup | Snapshots use a portable default location, UTC timestamps with microsecond uniqueness, generated-directory exclusions, symlink protection, and partial-archive cleanup. |
| Update | Removed hard-coded privileged ownership changes and arbitrary port killing; updates snapshot first, enforce Git safety, use the Compose adapter, and report snapshot recovery guidance on deployment or verification failure. |
| CLI | Decorated command failures now return exit code 1; health output includes counts and prioritized recommendations; Docker object construction no longer goes through the error decorator. |
| Documentation | Replaced the empty README with installation, command, configuration, safety, architecture, and development guidance. |
| Tests | Added nine regression tests covering configuration, discovery, Git state, backups, mapping, provider commands, health scoring, and analyzer isolation. |
| Repository hygiene | Removed the tracked `.venv`, bytecode caches, and generated egg metadata from the Git index; the existing ignore rules now protect the repository from reintroducing them. |

## Remaining considerations

The repository is now suitable for local development and deterministic unit-level verification. The only material follow-up work is environment-dependent integration coverage: a disposable Git remote fixture, a Docker daemon with a small Compose fixture, and CI jobs that exercise those integrations. Those tests are intentionally not required for the core package to install or for filesystem/Git-only commands to run. A future release may also add explicit archive-restore tooling, because the update command currently preserves a recovery snapshot and reports its path rather than silently overwriting live project files during automatic rollback.

## Production milestone update

The attached production brief identified safe update planning as the first milestone. That milestone is now implemented locally without touching the real VPS.

| Delivered capability | Implementation |
|---|---|
| Typed plan | `UpdatePlan` and `UpdateRisk` record project identity, runtime actions, Git state, health-before, snapshot requirement, risk, approval, and reasons. |
| Side-effect-free planning | `UpdatePlanner` performs discovery, Git analysis, and health-before analysis without snapshot, fetch, pull, Compose mutation, or runtime execution. |
| Dry-run | `aipm update PROJECT --dry-run` renders the plan, writes a `planned` audit record, and performs no state-changing operation. |
| Approval gate | Normal execution renders the plan and stops unless `--yes` is provided. Blocked and approval-required outcomes are audited. |
| Git safety | Untracked files are classified with modified files; critical infrastructure changes, conflicts, detached HEAD, and other review conditions prevent execution. Non-critical changes can be preserved in a named stash. Stash-apply conflicts preserve the stash and stop the transaction. |
| Execution boundary | Approved execution snapshots first, fetches/pulls only when a configured remote exists, runs the custom runtime or Compose adapter, and verifies health afterward. |
| Audit history | `AuditService` writes JSON records for plan, approval, blocked, failed, and successful outcomes under a configurable audit directory. |
| Verification | Regression coverage increased from nine to thirteen tests, including dry-run side-effect protection, approval gating, approved custom-runtime execution, and audit serialization. |

This milestone does not claim real-VPS readiness. The referenced VPS infrastructure and legacy scripts were absent from the sandbox, so production integration behavior remains unverified. The next milestone is read-only inspection of the real environment after credentials are rotated and the user explicitly authorizes that inspection; no state-changing VPS operation is implied by this local implementation.


## Current Mission Control checkpoint

**Updated:** 2026-08-24

The historical baseline and checkpoint below are retained for auditability. They describe the repository state at the earlier Phase 4B review; the current pushed checkpoint is maintained in [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md). The historical checkpoint was:

```text
HEAD=af1a10b1f150335df27fda5d915f44e4f14146f4
ORIGIN_MAIN=af1a10b1f150335df27fda5d915f44e4f14146f4
WORKTREE=CLEAN
```

**Historical Phase 4B position:** MC-6.13 Phases 2, 3, 4A, and 4B were complete at that earlier checkpoint. The later Phase 4C through 4E work is recorded in [`docs/MC-6.13_STATUS.md`](docs/MC-6.13_STATUS.md) and [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md). The advisor remains read-only, non-authoritative, and non-executing.

| Completed area | Current result |
|---|---|
| MC-1.5 through MC-5 | Read-only dashboard foundation, telemetry, event/incident projections, notification safety, and GET-only APIs. |
| MC-5.1 through MC-5.1.2 | SQLite `mode=ro`, `PRAGMA query_only=ON`, active-WAL compatibility, filesystem write denial, and service-template hardening. |
| MC-5 Gate 2.1 | Successful operator staging run preserved in `ops/staging/mc5-gate2.1-staging-v2.sh`. |
| MC-6.1 through MC-6.3 | Shared Observation contracts, frontend shell, scheduler, Server/Host Intelligence, and static-module routing. |
| MC-6.5 through MC-6.6.3 | Docker intelligence, project/application association, conservative taxonomy, filtering, and health-evidence UX. |
| MC-6.7 through MC-6.7.1 | Allow-listed Systemd observation with seven genuine units; Cloudflared remains Docker-owned. |
| MC-6.8 | Bounded, redacted, read-only Logs with symbolic sources, fixed adapters, bounded queries, protected cursors, and safe frontend rendering. |
| MC-6.13 Phase 2 | Immutable evidence normalization, mandatory evaluation time, freshness/availability semantics, deterministic canonical serialization, stable identifiers, and explicit uncertainty; pushed at `ebe1f84`. |
| MC-6.13 Phase 3 | Ten deterministic evidence-linked rules, canonical field schema, bounded continuity envelope, exact evidence binding, and explanatory non-executable recommendations; pushed at `a7ee2f1`. |
| MC-6.13 Phase 4A | Pure bounded request validation/snapshotting and direct normalizer-to-rule-engine composition; pushed at `37d8a0e`. |
| MC-6.13 Phase 4B | Private authenticated read-only `POST /api/advisor/evaluate`; bounded transport validation, fail-closed authentication, safe 400/401/422/500 errors, direct Phase 4A delegation, and existing `AdvisorResponse` serialization; pushed at `af1a10b`. |

MC-6.9 remains the next separate design/inspection milestone. MC-6.13 Phase 4B is complete as a private authenticated API boundary, while Phases 4C–4E remain future and unauthorized. It must not be interpreted as public exposure, live observation collection, dashboard UI integration, LLM/provider functionality, or action authority.

## Current operational gates

The successful Gate 2.1 harness remains byte-identical:

```text
SHA-256: 9e12cdc01f901381ff34b16dd68c11a14cf1158e1c32bbde928bce13c6c238e7
```

Repository completion does not imply target-VPS deployment or runtime validation. Separately supplied production verification reports that telemetry commit `0ab4b0e859fff96add058cb3eb55e0ff408b1a83` reduced the previous retention-spin CPU to approximately 0%, preserved five samples over five minutes, and produced no new lock errors after deployment. This is live VPS evidence, not a repository test result. The dashboard ingress architecture is:

```text
Cloudflared container
    -> 172.20.0.1:8788
    -> host nginx reverse proxy
    -> 127.0.0.1:8787
    -> AIPM dashboard
```

The dashboard remains loopback-bound and the public hostname is `vpanel.03092017.xyz`. Cloudflared and Docker remain infrastructure-owned. Credentials, live SQLite, Systemd runtime changes, telemetry runtime changes, and notification activation remain outside the MC-6.13 repository scope.

The complete current ledger and next-step sequence is maintained in [`docs/MC-6_STATUS.md`](docs/MC-6_STATUS.md). The dedicated advisor status is in [`docs/MC-6.13_STATUS.md`](docs/MC-6.13_STATUS.md). The broader update-management roadmap remains in [`PRODUCTION_ROADMAP.md`](PRODUCTION_ROADMAP.md).

## Historical verification baseline

The historical MC-6.13 Phase 4B validation passed 17 focused tests and 516 full tests. The current synchronized checkout subsequently reproduced 618 full tests and 73 tests for the latest SQLite correction, with the existing Starlette/httpx deprecation warning.

> **Historical project position:** This paragraph records the earlier Phase 4B checkpoint. It is superseded by [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md), which records MC-6.13 complete through bounded read-only Phase 4E, the live vpanel observations, and the continuing prohibition on MC-6.12 operational execution.

## Current-state reconciliation — 2026-08-28

The canonical current-status record is [`docs/CURRENT_STATUS.md`](docs/CURRENT_STATUS.md). The repository checkpoint is `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`, and local `HEAD`, `origin/main`, and remote `main` are equal with ahead/behind `0/0`, a clean worktree, and no staged files. The preservation stash `stash@{0}` remains intentionally untouched and contains the separate incident-reopen workstream plus `docs/MC-6.9_DESIGN.md`; those items are not part of published current main.

The read-only Mission Control cockpit is substantially landed and live. Fresh web inspection confirmed the dashboard, server, Docker, projects, bounded logs, incidents, history, settings posture, and read-only advisor surfaces. The advisor returned fresh aligned evidence with 18/18 coverage and six points spanning 300 seconds at 60-second cadence for CPU, memory, and disk. Live observations also show bounded stale/unavailable states, including stale MC-3 freshness, stale container resource observations, unavailable Systemd entries, and disabled/unavailable notification audit data. HTTP evidence does not establish the deployed Git commit, systemd unit contents, database ownership, producer convergence, or Cloudflare configuration; the live Settings surface reports `commit=Unknown`, `public_ingress=not_observed`, and `permanent_service=not_observed`.

MC-6.12 is foundation-only, not an operational action plane. No executor, action route/UI, durable operational state, leases/fencing, production target, service account, production authorization, autonomous remediation, LLM/provider execution, or notification delivery is enabled. Database merge/delete/repair/migration/rekey operations remain unauthorized.
