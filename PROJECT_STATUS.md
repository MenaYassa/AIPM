# AIPM Project Status

**Assessment date:** 2026-08-16
**Repository:** [MenaYassa/AIPM][1]
**Assessed branch:** `main` at `97e3d06` (`Refactor health engines, models, and providers`)

## Executive summary

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
