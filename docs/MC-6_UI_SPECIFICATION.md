# AIPM Mission Control MC-6 UI Specification

## Purpose

This specification defines the future information architecture and interaction model for the MC-6 cockpit. It is a design artifact only. It does not authorize implementation, deployment, public exposure, write actions, notification activation, or changes to the current dashboard service.

The existing MC-5 page remains the visual baseline. MC-6 should improve discoverability and scale without discarding the current overview, service pulse, Docker/container, project, history, handbook, event, incident, and notification-safety experiences.

## Product posture

Mission Control is an **observability cockpit**, not an unrestricted administration panel. Every page must make the following operational distinction visible:

> Current observation is not permission to change the system.

The first MC-6 release contains no start, stop, restart, update, shell, Docker exec, Compose mutation, Git pull, backup restore, notification test, incident acknowledgement, or systemd control. Any future action is displayed only as a FUTURE product concept until a separate approval/control-plane design exists.

## Information architecture

The top-level navigation is organized by operational domain rather than by backend implementation:

| Navigation item | Classification | First release posture |
|---|---|---|
| Dashboard | EXISTS / EXTEND | Preserve the current overview as the landing page. |
| Server | EXTEND | Add read-only server detail and capacity views. |
| Docker | EXISTS / EXTEND | Add container, image, volume, network, and bounded log views without lifecycle controls. |
| Projects | EXISTS / EXTEND | Add project detail, Git posture, health, and runtime inventory without update controls. |
| Systemd | NEW | Add read-only unit inventory and health details. |
| Logs | NEW | Add bounded, redacted logs from approved sources. |
| Incidents | EXISTS / EXTEND | Expand Incident Room, timeline, evidence, and filtering without acknowledgement controls in the first MC-6 slice. |
| History | EXISTS / EXTEND | Add chart comparison, range presets, downsampling, and freshness context. |
| Notifications | EXISTS / EXTEND | Show safety posture, audit, metrics, channels, and policies without secrets or activation controls. |
| Settings | EXTEND | Show sanitized effective settings and deployment posture; never expose raw configuration or credential values. |
| AI Agent | FUTURE | Reserved for an authenticated, approval-gated advisor/control plane. |

The desktop shell uses a left navigation rail or compact top navigation, a persistent status strip, a main content region, and a contextual detail drawer. On mobile widths, the rail becomes a menu and detail drawers become full-screen panels.

## Page hierarchy

### Dashboard

The Dashboard retains the MC-5 composition:

1. System overview cards for CPU, memory, disk, load, uptime, and network.
2. Service Pulse for telemetry and MC-3 freshness.
3. Docker/container summary grouped by project.
4. Resource History with range controls.
5. Project inventory and project posture.
6. Local tunnel visibility.
7. Handbook and operator posture.
8. MC-3 Event Stream.
9. Incident Room summary.
10. Notification Safety and audit metrics.

The Dashboard is a summary, not a second copy of every page. Each card links to the corresponding domain page or filtered detail view.

### Server

The Server page is read-only and contains:

- Host identity and AIPM version.
- CPU topology and current load.
- Memory, swap, and disk/filesystem capacity.
- Network interface and connection summary.
- Uptime and observation timestamps.
- Freshness and availability for each measurement.
- Safe warnings for capacity pressure.

It must not show arbitrary environment variables, credentials, full process command lines, or unrestricted filesystem listings.

### Docker

The Docker page contains a project-grouped container table and detail view:

- Container name and stable ID display suffix.
- Compose project and service labels.
- Image reference, state, health, restart count, start time, ports, and safe labels.
- CPU/memory resource samples and freshness.
- Image, volume, and network inventory where the provider can supply it safely.
- Bounded recent logs through the Logs page or detail drawer.

The first version has no lifecycle buttons. A stopped or unhealthy container is an observed state and links to an incident or handbook route, not to an action.

### Projects

The Projects page contains:

- Discovered project path and display name.
- Git branch, local commit, dirty/conflict state, and known ahead/behind state.
- Compose/runtime presence and container grouping.
- Health report summary and finding severities.
- Latest telemetry and event timestamps.
- Links to history, events, and incidents.

The UI must state that ahead/behind is based on already-known remote-tracking state. It must not imply that the dashboard fetched a remote. Git pull/update controls are FUTURE.

### Systemd

The Systemd page is a read-only inventory of allow-listed user or service-manager units:

- Unit name, description, load state, active state, substate, enablement state, and timestamps.
- Main process ID only where safe and necessary.
- Restart count or failure indication if available from structured status.
- Dependency/ordering summary with bounded depth.
- Unit-specific health link to telemetry or incidents.

The page must never display arbitrary unit environment values, credentials, full `ExecStart` command lines containing secrets, or mutation controls. The first adapter should use structured systemd queries rather than shell concatenation.

### Logs

The Logs page provides bounded read-only observations:

- Source selector limited to an allow-list of AIPM, telemetry, events, dashboard, and approved project sources.
- Time range, severity, unit/project filter, and maximum line count.
- Truncated line display with explicit truncation marker.
- Redaction before serialization and before browser rendering.
- Correlation links to events and incidents where an existing identifier is available.

Arbitrary path input, arbitrary shell commands, Docker exec, journal export, file download, and unbounded tailing are not part of the first release.

### Incidents

The Incidents page expands the current Incident Room:

- Open, acknowledged, resolved, and historical filters.
- Severity and resource filters.
- Correlation key and lifecycle timeline.
- Event evidence and source run references.
- First/last observed timestamps and age.
- Explicit state explanation when evidence is unavailable.

The first MC-6 release does not expose acknowledgement or action controls. Existing MC-3 acknowledgement capability is treated as outside the current UI contract until a separate authorization review approves it.

### History

The History page supports:

- Host CPU, memory, disk, load, network, and swap trends.
- Container resource and lifecycle trends.
- Project and tunnel history.
- Range presets `1h`, `6h`, `24h`, `7d`, and custom bounded windows later.
- Overlay comparisons for one metric at a time.
- Sparse-data, stale, unavailable, and never-sampled states.
- Explicit sample timestamps and retention context.

Charts must never interpolate missing values as healthy current state. Hover or accessible text must identify the sampled time and freshness status.

### Notifications

The Notifications page is a safety and audit surface:

- Effective enabled state.
- Number of configured/enabled channels and policies.
- Safe channel metadata: ID, name, type, supported/configured booleans.
- Policy metadata: severity threshold, transitions, selected channel IDs, cooldown/window/maximum values.
- Delivery audit status and suppressed/unknown/retry metrics.
- Incident and event links.

The UI must never render secret references, environment variable names, destination values, webhook URLs, tokens, or provider payloads. It must not contain “send test”, “enable”, or “activate” controls.

### Settings

The first Settings page is a sanitized posture view:

- Dashboard bind and deployment posture.
- Telemetry interval, slow-task intervals, stale thresholds, sampling mode, and retention policy.
- Event processor interval and retention policy.
- Notification enabled state and safe counts.
- Read-only boundary status and required filesystem protection status when exposed by a safe server capability.
- Version and repository commit.

Settings are not editable. Raw YAML and environment values are never returned. A later write-enabled settings flow is FUTURE.

### AI Agent

The AI Agent page is a reserved FUTURE area. It must not be rendered as if an agent can act in MC-6.1. The eventual design requires task intent, evidence, proposed plan, risk classification, user approval, execution sandbox, audit record, result verification, and rollback state.

## Reusable UI components

| Component | Responsibilities | Safety requirement |
|---|---|---|
| `AppShell` | Navigation, page title, global status strip, responsive layout. | No domain fetching or action logic. |
| `StatusRail` | Repository commit, dashboard availability, overall service state, last refresh. | Never infer healthy from missing data. |
| `MetricCard` | One metric, unit, timestamp, freshness, and safe error state. | Distinguish `0` from unavailable. |
| `FreshnessBadge` | `fresh`, `stale`, `unavailable`, `never_sampled`, `unknown`. | Use domain state exactly; no silent fallback. |
| `AvailabilityPanel` | Provider/database/service unavailable explanation. | Show safe diagnostic only. |
| `FilterBar` | Bounded query filters and range selectors. | Validate and cap values client and server side. |
| `DataTable` | Sortable, responsive tabular data. | Redact unsafe fields and support keyboard navigation. |
| `Timeline` | Events, evidence, incident transitions, log correlation. | Preserve event ordering and stable IDs. |
| `TrendChart` | Historical series with gaps and freshness. | Never fabricate samples or hide gaps. |
| `DetailDrawer` | Contextual detail without losing page state. | No hidden action buttons. |
| `EmptyState` | No records, never sampled, disabled, or no configured channels. | Explain the semantic reason, not just “empty.” |
| `ErrorState` | Safe failure message and retry/refresh suggestion. | No tracebacks, secrets, SQL, or raw command output. |
| `ReadOnlyBanner` | Explains that Mission Control is observation-only. | Persistent on pages that could be mistaken for administration. |
| `RedactedCodeBlock` | Safe handbook/config excerpts. | Allow-list content; never display raw environment. |

## Frontend state model

The client maintains one normalized read state per resource family. A resource record contains:

```text
status: loading | ready | stale | unavailable | never_sampled | error
fetched_at: timestamp | null
observed_at: timestamp | null
request_id: opaque local identifier
data: typed payload | null
error: safe code/message | null
```

The state model must separate transport state from observation state. A successful HTTP response can contain `available=false`, `status=unavailable`, or a stale observation. A network error is not the same as a healthy empty result.

The scheduler owns polling and exposes:

```text
start(resource, cadence)
stop(resource)
refresh(resource)
visibility_pause()
visibility_resume()
```

There must be one timer per resource family, no timer creation inside render functions, no overlapping requests for the same resource, and bounded retry/backoff for temporary transport failure. Manual refresh remains a read request and must not trigger telemetry sampling.

## Polling and future streaming

Initial cadences preserve MC-5 behavior:

| Resource | Cadence |
|---|---:|
| Overview | 15 seconds |
| Service Pulse | 15 seconds |
| Events | 15 seconds |
| Incidents | 30 seconds |
| Notifications/metrics | 30 seconds |
| History | 60 seconds or manual range change |
| Static inventory | On page entry and manual refresh |

SSE may later replace event/incident polling for authenticated or loopback use. The client must support a replay cursor and reconnect state before SSE is considered. WebSockets remain FUTURE.

## Responsive and accessibility requirements

The UI must be verified at desktop, tablet, and mobile widths. Cards collapse into single-column sections; dense tables become horizontally scrollable or transform into labeled rows; charts provide textual summaries; color is never the sole state indicator; all controls have keyboard focus styles; and status badges include accessible labels.

The dashboard must remain readable under long project paths, long container names, missing data, high event volume, and narrow terminal-like browser windows. The UI should use a restrained visual hierarchy: critical operational state first, trend context second, explanatory handbook content third.

## Navigation and URL strategy

The first migration may remain a single-page application with hash or query-based section selection. A later static multi-page or client-router structure is acceptable if it preserves direct navigation and does not introduce a server-side session requirement.

Suggested stable paths are:

```text
/
/server
/docker
/projects
/systemd
/logs
/incidents
/history
/notifications
/settings
```

These are presentation paths. API paths remain under `/api` and should not be renamed as part of the first UI migration.

## Frontend technology decision

| Option | Decision | Rationale |
|---|---|---|
| Current vanilla HTML/CSS/JS | **SELECT for MC-6.1–MC-6.3** | Lowest memory and deployment cost, no build toolchain, already validated, easy loopback serving, and sufficient for incremental extraction and component discipline. |
| HTMX/server-rendered | **NOT SELECTED initially** | Could reduce client JavaScript but would move more state/rendering into FastAPI and complicate charting, polling coordination, and shared Web/TUI semantics. Re-evaluate only for a content-heavy admin surface. |
| React + Vite | **DEFERRED** | Strong component/testing ecosystem and future interaction support, but adds Node/build artifacts, deployment complexity, bundle/runtime overhead, and migration risk before the information architecture is stable. |
| Another lightweight framework | **DEFERRED** | No current requirement justifies an additional dependency while vanilla extraction remains adequate. |

The choice is not a rejection of React. It is a sequencing decision: stabilize backend contracts and information architecture first, then reconsider a framework migration with measured bundle size, memory, test, and deployment costs.

## UI test strategy

Every page and component needs:

- Fixture-driven rendering tests for fresh, stale, unavailable, never-sampled, empty, and error states.
- Contract tests against representative API payloads and unknown additive fields.
- Secret scanner tests for rendered HTML and serialized response data.
- Polling tests proving one timer per resource and no request storm.
- Responsive visual acceptance at 1440×1000, 1024×900, 768×1024, and 390×844.
- Keyboard and accessible-name checks for navigation, filters, tables, and drawers.
- Negative tests proving no POST/action/acknowledgement controls are present in the first release.
- Failure tests for unavailable API, partial provider availability, stale observations, and long-running history queries.

## Current implementation status

**Updated:** 2026-08-24

The approved vanilla static architecture remains implemented through MC-6.8. The repository also contains MC-6.13 Phase 2/3/4A pure advisor domain logic and the private authenticated Phase 4B advisor API at `af1a10b`, but no advisor UI was added. The delivered navigation covers Dashboard, Server, Docker, Projects, Systemd, Logs, Incidents, History, Notifications, Settings, and the reserved AI Agent area. Dashboard, Server, Docker, Projects, Systemd, and Logs are functional observation pages; the remaining reserved pages retain safe placeholders or existing read-only projections where their milestone is not yet implemented.

The frontend uses the existing hash router, shared state helpers, centralized scheduler, `/static` module mount, escaped rendering, and explicit fresh/stale/unavailable/error/empty/unknown semantics. MC-6.8 adds one bounded Logs scheduler resource, backend-owned source selection, visible truncation/redaction state, and no download, stream, lifecycle, acknowledgement, or mutation controls.

MC-6.13 Phase 2/3/4A add no UI; Phase 4B adds only a private authenticated API transport boundary, not an advisor view, scheduler, LLM, or action surface;

## References

[1]: ../src/aipm/dashboard/static/index.html "Current MC-5 single-file frontend"
[2]: ../src/aipm/dashboard/server.py "Current FastAPI route adapter"
[3]: ../src/aipm/capabilities/dashboard/api.py "Overview capability façade"
[4]: ../src/aipm/capabilities/dashboard/service_health_api.py "Service freshness projection"
[5]: ../src/aipm/models/telemetry.py "Telemetry and freshness domain models"
[6]: MISSION_CONTROL.md "Mission Control UI, API, and safety history"
[7]: ../README.md "Existing AIPM commands and deployment posture"

## Classification summary

- **EXISTS:** current Dashboard sections, service pulse, Docker/container overview, project inventory, history, event stream, Incident Room, notification safety, search/filtering, and refresh behavior.
- **EXTEND:** navigation shell, detail views, charts, filtering, settings posture, accessibility, component extraction, and responsive behavior.
- **NEW:** Systemd page, Logs page, shared scheduler module, bounded log UI, and future TUI presentation.
**FUTURE:** MC-6.13 Phase 4C+ advisor UI integration, public API exposure, action controls, new authentication or ingress changes, SSE/WebSockets, and any write-enabled settings or operations. Phase 4B’s private API is not a UI feature.
