# AIPM

AIPM is a local **AI Platform Manager** for discovering and operating projects that use Git and Docker Compose. It provides a Typer-based command-line interface for host diagnostics, project discovery, Compose lifecycle operations, Docker inspection, Git safety checks, health reports, configuration snapshots, and guarded updates.

## Current status

The repository includes the core CLI, domain models, service/provider architecture, Git and Docker integrations, health analyzers, backup snapshots, and a guarded update workflow. The implementation is designed to remain useful on machines without a running Docker daemon: configuration, help, host diagnostics, and Git-only discovery do not require Docker. Docker- or Compose-specific commands return a clear error when the required runtime is unavailable.

For the detailed implementation assessment and remaining production considerations, see [`PROJECT_STATUS.md`](PROJECT_STATUS.md).

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

The tests focus on deterministic domain logic, configuration, discovery, backups, mapping, provider command construction, health aggregation, dry-run side-effect protection, approval gating, update execution, and audit serialization. Thirteen tests currently pass. Docker integration tests and disposable Git-remote tests should be added separately for environments that provide those fixtures.

## License

See [`LICENSE`](LICENSE).
