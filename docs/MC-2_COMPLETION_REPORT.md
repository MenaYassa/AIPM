# Mission Control MC-2 Completion Report

## 1. Completion status

MC-2 historical telemetry is implemented on top of the accepted MC-1.5 typed `DashboardSnapshot` architecture. The implementation is observation-only and persists only to AIPM’s own SQLite telemetry database.

MC-2 stops before Incident Room, events, alerts, notifications, AI diagnostics, guarded operational controls, update execution, Cloudflare API access, and lifecycle identity modeling.

> The sampler consumes `DashboardSnapshot`; it does not call psutil, Docker, Git, Compose, systemd, or Cloudflare directly.

## 2. Architecture before and after

### Before MC-2

```text
Current infrastructure
      ↓
DashboardTelemetryService
      ↓
DashboardSnapshot
      ↓
DashboardResponseMapper
      ↓
Mission Control overview API/UI
```

MC-1.5 provided reliable current-state observation but no persistence or independent sampling process.

### After MC-2

```text
Current infrastructure
      ↓
DashboardTelemetryService
      ↓
DashboardSnapshot
      ↓
TelemetryHistoryMapper
      ↓
TelemetrySampler
      ↓
SQLiteHistoryRepository
      ↓
SQLite database
      ↓
HistoricalQueryService
      ↓
HistoryResponseMapper
      ↓
Mission Control history API/UI
```

The web server does not start a sampler. Production uses one explicit `aipm telemetry run` process managed by systemd. The sampler handles SIGTERM/SIGINT by requesting a stop, allowing a current database transaction to finish and closing the connection before process exit.

## 3. Exact files created

| File | Responsibility |
|---|---|
| `src/aipm/models/history.py` | Typed historical points, query models, sample-run records, normalized sample bundle, results, and safe history responses. |
| `src/aipm/repositories/__init__.py` | Repository package boundary. |
| `src/aipm/repositories/telemetry/__init__.py` | Telemetry repository exports. |
| `src/aipm/repositories/telemetry/base.py` | `HistoryRepository` protocol. |
| `src/aipm/repositories/telemetry/sqlite.py` | Standard-library SQLite schema, normalized writes, queries, indexes, WAL/foreign-key setup, and retention cleanup. |
| `src/aipm/mappers/telemetry_history.py` | Snapshot-to-normalized-history mapping and safe history response serialization. |
| `src/aipm/services/telemetry/sampler.py` | Observation-only one-shot collection, mapping, persistence, and retention cycle. |
| `src/aipm/services/telemetry/runner.py` | Single-process interval runner with graceful signal handling. |
| `src/aipm/services/telemetry/history.py` | UTC range/limit validation and historical query service. |
| `src/aipm/capabilities/telemetry/__init__.py` | Telemetry capability exports. |
| `src/aipm/capabilities/telemetry/commands.py` | `sample` and `run` CLI command wiring. |
| `src/aipm/capabilities/dashboard/history_api.py` | Safe dashboard history façade. |
| `tests/test_history_repository.py` | Temporary SQLite schema, write/query, multiple-container, pragma, range, limit, retention, and corruption tests. |
| `tests/test_sampler.py` | Disabled telemetry, sampler/database failure, partial snapshots, signal shutdown, and mutation-boundary tests. |
| `tests/test_history_api.py` | Range validation, no-data, database failure, safe API, and response tests. |
| `tests/test_telemetry_config.py` | Safe defaults, invalid configuration, and environment override tests. |
| `scripts/measure_telemetry_storage.py` | Actual-schema temporary-database storage measurement. |
| `docs/MC-2_COMPLETION_REPORT.md` | This report. |

## 4. Exact files modified

| File | MC-2 change |
|---|---|
| `src/aipm/models/config.py` | Added `TelemetryConfig` with enabled, interval, retention, and database path settings. |
| `src/aipm/core/config.py` | Loads and validates the optional telemetry section and supports `AIPM_TELEMETRY_DB`. |
| `src/aipm/cli/app.py` | Registers `aipm telemetry sample` and `aipm telemetry run`. |
| `src/aipm/capabilities/dashboard/api.py` | Adds optional history façade wiring while keeping sampler construction independent from history API construction. |
| `src/aipm/dashboard/server.py` | Adds safe history routes and preserves `/healthz`, `/api/overview`, and `/`. No SQL was added. |
| `src/aipm/dashboard/static/index.html` | Adds only the minimal Historical pulse panel with CPU, memory, disk trends and 1H/6H/24H selectors. |
| `config/aipm.yaml` | Documents telemetry configuration defaults. |
| `README.md` | Documents MC-2 commands and history endpoints. |
| `docs/MISSION_CONTROL.md` | Documents MC-2 architecture, configuration, schema, retention, sampler, systemd, API, and UI behavior. |

Files created during MC-1.5 and retained without unnecessary redesign include the existing typed current-telemetry models/services, Docker provider boundary, dashboard mapper, handbook interface, and read-only overview API.

## 5. Exact files removed

No files were removed.

## 6. Final SQLite schema

The database contains five normalized tables.

| Table | Purpose | Important columns |
|---|---|---|
| `sample_runs` | One row per sampler cycle and component availability state. | `id`, `sampled_at`, `host_available`, `docker_available`, `projects_available`, `tunnel_state`, `duration_ms` |
| `host_samples` | One normalized host measurement row per cycle. | CPU, load, memory, swap, disk, network, `available`, `run_id`, `sampled_at` |
| `container_samples` | One row per observed container per cycle. | `container_id`, `container_name`, image, state, health, stack, restart count, CPU, memory, stats availability, `run_id`, `sampled_at` |
| `project_samples` | One row per discovered project per cycle. | name, path, branch, Git/Compose flags, dirty, ahead, behind, `run_id`, `sampled_at` |
| `tunnel_samples` | One local cloudflared observation per cycle. | state, source, systemd, local container names, `run_id`, `sampled_at` |

All tables use `INTEGER PRIMARY KEY AUTOINCREMENT` row identifiers. Child rows reference `sample_runs(id)` with `ON DELETE CASCADE`. UTC epoch seconds are stored internally; timezone-aware UTC datetimes are used by domain and API boundaries.

No opaque `DashboardSnapshot` JSON blob, Incident/Event tables, alert tables, AI output, or operational command state is stored.

## 7. Indexes

The schema creates the following indexes:

```text
idx_sample_runs_sampled_at
idx_host_samples_sampled_at
idx_container_samples_identity_time       (container_id, sampled_at)
idx_container_samples_name_time           (container_name, sampled_at)
idx_project_samples_name_time             (name, sampled_at)
idx_tunnel_samples_sampled_at
```

SQLite foreign keys are enabled for every repository-managed connection. WAL mode is enabled where supported. SQL values are parameterized. Each repository operation uses one short-lived connection and transaction; there is no connection pool and no unnecessary async database abstraction.

## 8. Retention behavior

Retention is deterministic and timestamp-based. After a successful sample write, the sampler computes:

```text
cutoff = sampled_at - retention_days
```

It deletes rows where each table’s `sampled_at` is older than the cutoff. Cleanup deletes child tables before `sample_runs` and never uses sample-run IDs as a time proxy. It modifies only the telemetry database and does not delete current state or files outside the telemetry database.

The safe default is one day of retention. `retention_days` must be greater than zero.

## 9. Sampler lifecycle

The sampler flow is:

```text
collect DashboardSnapshot
      ↓
map typed snapshot to normalized rows
      ↓
write sample_runs + host/container/project/tunnel rows
      ↓
delete rows older than timestamp cutoff
      ↓
return SampleResult
```

`aipm telemetry sample` executes one cycle. `aipm telemetry run` executes one cycle, waits for the configured interval, and repeats. The runner’s default wait is interruptible through an internal event. SIGTERM and SIGINT set the stop flag; the current sampler transaction is not forcibly interrupted.

Disabled telemetry returns a skipped result and does not collect or write. Sampler, database, partial-snapshot, and retention failures are logged through the shared AIPM logger and returned as safe statuses. Current `/api/overview` does not depend on the history repository and continues working if SQLite is unavailable.

## 10. Systemd unit/template

The production design uses one explicit sampler process. MC-2 did not install or enable any systemd unit.

```ini
[Unit]
Description=AIPM Mission Control telemetry sampler
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=aipm
WorkingDirectory=/opt/AIPM
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/AIPM/.venv/bin/aipm telemetry run
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
```

The sampler is not started from FastAPI, so multiple web workers cannot silently create duplicate sampling loops.

## 11. CLI commands

```text
aipm telemetry sample
```

Collects and persists one read-only sample.

```text
aipm telemetry run
```

Runs the single long-lived read-only sampler process intended for systemd.

Existing commands, including `aipm dashboard`, remain available and observational.

## 12. API endpoints

Existing endpoints are preserved:

```text
GET /healthz
GET /api/overview
GET /
```

MC-2 adds:

```text
GET /api/history/host?range=1h&limit=500
GET /api/history/containers?name=<container>&range=24h&limit=500
GET /api/history/projects?name=<project>&range=24h&limit=500
GET /api/history/tunnel?range=24h&limit=500
```

Supported ranges are `1h`, `6h`, `24h`, and `7d`; valid limits are 1–5000. Invalid ranges/limits and repository failures produce safe structured responses containing `available`, `status`, `error`, and `points`. SQL errors, tracebacks, private paths, and database implementation details are not exposed.

## 13. UI changes

The existing Mission Control visual design was preserved. A single **Historical pulse** card was added to the main stack. It shows minimal inline SVG trends for CPU, memory, and disk and provides `1H`, `6H`, and `24H` range selectors. It displays an unavailable state when SQLite/history is unavailable.

No Incident Room, alerts, notifications, controls, log streaming, AI diagnostics, or visual redesign was added.

## 14. Test results

The final suite result is:

```text
51 passed, 1 warning in 1.41s
```

The warning is the existing Starlette/httpx test-client deprecation warning from the installed dependency stack. It does not fail the suite.

The tests use temporary SQLite databases and mocked/fake infrastructure services. Coverage includes database initialization, schema creation, foreign-key/WAL setup, normalized writes, multiple containers, time-range filtering, limits, retention, disabled telemetry, sampler/database failure, partial snapshots, no data, safe history API failure, signal shutdown, corrupted database handling, and mutation-boundary scans.

`git diff --check` passed. No production VPS infrastructure was modified. No Docker, Compose, Git, systemd, package, firewall, Cloudflare, or external service mutation occurred.

## 15. Storage measurement

Storage was measured with `scripts/measure_telemetry_storage.py` using the actual production SQLite schema, 120 persisted cycles, and a 15-second interval in a temporary database. The observed representative environment contained two discovered projects and zero running containers; the script records entity counts rather than using a theoretical row-size estimate.

| Measurement | Result |
|---|---:|
| Sampling interval | 15 seconds |
| Samples per day | 5,760 |
| Representative cycles inserted | 120 |
| Host rows per cycle | 1 |
| Container rows per cycle | 0 |
| Project rows per cycle | 2 |
| Tunnel rows per cycle | 1 |
| Schema/database bytes before samples | 53,248 bytes |
| Database size after representative sampling | 90,112 bytes |
| Measured growth over 120 cycles | 36,864 bytes |
| Measured growth per cycle | 307.2 bytes |
| Projected 24-hour growth | 1,769,472 bytes |
| Projected 7-day growth | 12,386,304 bytes |
| Projected 30-day growth | 53,084,160 bytes |

The measured values include the actual schema and indexes. They are a baseline for the measured entity counts above; production growth will increase with the number of containers and projects sampled per cycle. A production VPS should run the same script against representative counts before selecting a longer retention period.

## 16. Known limitations

MC-2 stores current observations, not lifecycle events. A container recreation may produce rows with different `container_id` values, but the system does not yet derive creation, disappearance, restart, or replacement events. That identity/event model is intentionally deferred to MC-3.

The first history API returns raw sample points within bounded ranges. Resolution/aggregation is not yet implemented, so long ranges and high entity counts should be evaluated before expanding retention.

The minimal UI currently proves host history only. Container and project history are available through the API but do not yet have dedicated trend panels.

Sampling remains request-independent only when `aipm telemetry run` is actually deployed as the single systemd-managed process. MC-2 did not install or enable that service on a VPS.

The storage measurement was performed in the sandbox with zero running containers. Actual VPS measurements should be repeated with its real container/project counts.

## 17. Technical debt

The SQLite repository currently initializes the database in its constructor and opens one short-lived connection per operation. This is intentionally simple and safe for MC-2, but higher-frequency or multi-process history workloads may benefit from explicit lifecycle ownership and checkpoint policy.

History API range resolution is limited to fixed windows without downsampling. A later milestone should add deterministic aggregation before increasing retention or exposing dense charts.

The existing installed Starlette/httpx test-client warning should be resolved as a separate dependency maintenance task.

The static UI remains a single HTML/JavaScript asset. It is appropriate for the current proof milestone but may become difficult to extend when Incident Room and event workflows are added.

## 18. Recommended MC-3 architecture

MC-3 should add a separate deterministic event derivation layer over historical rows and current snapshots:

```text
HistoryRepository + current DashboardSnapshot
      ↓
EventDerivationService
      ↓
EventRepository
      ↓
Incident Room query service/API
      ↓
Mission Control incident UI
```

MC-3 should model container lifecycle identity, state transitions, threshold evaluation, event severity, acknowledgement, and incident grouping as separate domain concepts. Alerts, notifications, AI explanations, and guarded controls should remain later capabilities that consume deterministic events rather than being embedded in the sampler or history repository.

## 19. Stop condition

MC-2 is complete. No MC-3 implementation has been started, and no further automatic work should proceed without a new explicit instruction.
