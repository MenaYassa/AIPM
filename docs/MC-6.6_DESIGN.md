# MC-6.6 Design — Project & Application Intelligence

**Status:** Design and inspection only
**Baseline:** `11ff4609eeb3bda20f96d693a695d4c878804683`
**Milestone:** MC-6.6
**Implementation:** Not started

## 1. Executive summary

MC-6.6 extends Mission Control from a container-oriented view into a bounded, read-only view of the applications and platforms running on the VPS. The user-facing goal is to answer **“which applications are running, what components belong to each application, and what evidence supports their current health?”** rather than presenting every container as an unrelated item.

The milestone is an extension of existing project discovery, Git snapshots, Compose/runtime observations, Docker telemetry, health analyzers, and the Mission Control frontend shell. It is not a second Docker collector, a deployment manager, a Compose controller, or an AI-agent action surface. Docker remains the authoritative runtime observation source for container state and resource data; the existing project service remains the authoritative local project-discovery source; the new capability is a bounded aggregation and presentation layer that correlates those sources.

The implementation must remain strictly read-only. The future façade may inspect local project metadata, local Git posture, known remote-tracking state already present locally, Compose metadata/status, Docker container observations, telemetry, and health findings. It must never fetch, pull, checkout, stash, reset, clean, update, execute arbitrary project scripts, mutate Docker state, write SQLite, activate notifications, or expose secrets.

## 2. Exact roadmap objective and reconciliation

The authoritative MC-6 roadmap defines MC-6.6 as **“Projects”** and requires project detail and navigation from the existing project inventory, Git snapshots, health analyzers, Compose status, and telemetry. Its explicit boundary allows inspection of local branch state, dirty/conflict state, known remote-tracking state, Compose status, and health findings, while forbidding Git mutation and arbitrary project scripts.

MC-6.5 already delivered Docker Intelligence: bounded GET-only Docker summary, container detail, image/volume/network inventory, project-key grouping, observation semantics, safe mappers, and the `#/docker` page. Therefore MC-6.6 must reuse those capabilities and add the missing project/application correlation layer. It must not reimplement Docker observation, create a second project inventory, or duplicate the Server & Host Intelligence capability delivered in MC-6.3.

| Concern | MC-6.5 state | MC-6.6 decision |
|---|---|---|
| Docker current state and resources | Exists through `DockerTelemetryService` and `DashboardDockerApi` | Reuse; do not create another collector |
| Bounded Docker detail and inventory | Exists through `DockerObservationService` and `DockerDetailMapper` | Reuse; add only project correlation |
| Minimal Docker project grouping | Exists through `project_key`, `service_name`, Compose labels where available, and Docker API groups | Extend into a higher-level project/application model |
| Local project discovery | Exists through `ProjectService.discover()` | Extend with stable identity and detail aggregation |
| Git posture | Existing Git provider/model snapshot seam | Extend through a read-only project façade; no remote fetch |
| Compose metadata/status | Existing project/Compose providers and service models | Extend with bounded, read-only association and evidence |
| Project telemetry/history | Existing project telemetry and history routes | Reuse and correlate freshness; do not create another database or sampler |
| Health aggregation | Existing analyzers and health concepts exist, but no Mission Control project contract | New bounded aggregation contract over existing evidence |
| Project detail façade | No dedicated Mission Control façade | New read-only façade |
| Project API routes | No functional project page/API contract in the current shell | New additive GET-only routes |
| Project frontend page | `#/projects` remains a placeholder | Extend to functional inventory/detail views |
| Arbitrary application discovery | Not reliable from Docker data alone | Explicitly unavailable; show unknown/ungrouped with evidence |

## 3. Current architecture inspection

### 3.1 Docker provider, service, observation, and models

The current Docker path is layered. `DockerTelemetryService` supplies fast resource/state snapshots, while `DockerObservationService` supplies bounded point-in-time container and inventory observations. `DashboardDockerApi` is the dashboard façade and deliberately exposes no lifecycle methods. `DockerDetailMapper` converts provider objects into bounded public structures.

The façade already normalizes container identity, state, health, restart count, ports, networks, mount kinds, and resource freshness. It also computes a minimum grouping view using `project_key`, with `ungrouped` as the safe fallback. MC-6.6 should consume this normalized output or the same underlying typed observations through a dedicated project façade; it must not inspect raw Docker SDK objects or add an arbitrary Docker query mechanism.

### 3.2 Existing project discovery

`ProjectService.discover()` scans configured discovery paths, applies configured ignore directories, honors maximum depth and symlink policy, and treats a discovered repository as a scan boundary. A directory becomes a project when it contains one or more recognized Compose files and/or a `.git` directory. The current `Project` model contains a name, path, capability flags, Compose file paths, optional Compose services, optional Git metadata, and a legacy health enum.

This is a local filesystem discovery boundary, not a runtime project registry. It does not guarantee that every running container belongs to a discovered project, and it does not prove that a directory name equals a Docker Compose project name. MC-6.6 must preserve those distinctions.

### 3.3 Git posture

The existing Git provider and `GitRepository` model provide a local snapshot seam for branch and dirty state. MC-6.6 may expose safe local posture such as current branch, detached-head state if represented, dirty/conflict indicators, and known local remote-tracking state. The façade must not invoke fetch, pull, checkout, stash, reset, clean, update, or any operation that changes Git state. It must not expose arbitrary remotes, credentials, environment files, or unbounded command output.

Where a Git attribute cannot be collected safely or is unavailable, the API must return an explicit unavailable or unknown observation with a safe reason code rather than guessing.

### 3.4 Dashboard shell and scheduler

The frontend is a vanilla JavaScript shell with hash navigation, a shared sidebar, responsive layout, centralized polling, and normalized observation state. The existing route is `#/projects`, currently a placeholder. MC-6.6 should replace only that view and add project-specific scheduler resources using the existing scheduler and `/static` module mount convention. No framework, package manager, build step, or second polling loop should be introduced.

Polling must remain resource-scoped and visibility-aware. Project inventory and selected project detail should not create duplicate timers when navigation changes. The selected project route must be bounded and should stop or pause detail polling when the user leaves the page or the document is hidden.

### 3.5 Server Intelligence and existing telemetry/history

MC-6.3 already provides the Server & Host Intelligence contract. MC-6.6 should link project evidence to host observations only where an existing safe contract already supplies the relationship; it must not duplicate host collection. Existing project telemetry/history and container-resource history remain authoritative for historical readings and freshness. The project façade should reference existing observation metadata and history links rather than creating a project database, worker, or sampling loop.

## 4. Capability classification

| Capability | Classification | Design position |
|---|---|---|
| Discover local Compose/Git-backed projects | **EXISTS** | Reuse `ProjectService` and configured discovery policy |
| Read local Git branch/dirty/conflict posture | **EXTEND** | Add a safe snapshot adapter/mapper over the existing Git provider seam |
| Read known local remote-tracking posture | **EXTEND** | Expose only locally known tracking state; never contact a remote |
| Read Docker container state/resources | **EXISTS** | Reuse MC-6.5 observation and telemetry contracts |
| Read Compose service associations | **EXTEND** | Normalize known service/project labels and local Compose metadata |
| Correlate runtime containers to projects | **NEW** | Add deterministic correlation rules with confidence/evidence |
| Aggregate project health | **NEW** | Add evidence-based health aggregation; no invented health |
| Project detail GET façade | **NEW** | Add bounded `DashboardProjectApi` or equivalent façade |
| Project list/detail/health routes | **NEW** | Add only additive GET routes |
| Functional `#/projects` inventory | **NEW** | Replace the existing placeholder with vanilla JS views |
| Logs for project health | **UNAVAILABLE in MC-6.6** | Defer to MC-6.8; expose only a safe link/state if applicable |
| Systemd unit associations | **UNAVAILABLE in MC-6.6** | Defer to MC-6.7; do not infer systemd state |
| Project mutation or remediation | **UNAVAILABLE and forbidden** | No action controls or mutation routes |
| Automatic semantic discovery of every application | **UNAVAILABLE** | Use explicit unknown/ungrouped fallback |

## 5. Project Intelligence model

The future model should distinguish a **logical project/application**, its **runtime components**, and the **evidence** used to associate and assess them. A Docker Compose project may be a useful runtime grouping, but it is not automatically a product-level application. The API should make that distinction visible rather than collapsing all names into one field.

A proposed typed contract is:

```text
ProjectApplication
  id: opaque stable local identifier
  display_name: safe display label
  source: discovered | runtime_group | manual_mapping | ungrouped
  confidence: exact | probable | inferred | unknown
  local_project: optional ProjectReference
  runtime_group: optional RuntimeGroupReference
  components: bounded list[ProjectComponent]
  git: Observation[GitPosture]
  compose: Observation[ComposePosture]
  runtime: Observation[RuntimePosture]
  health: ProjectHealth
  telemetry: Observation[ProjectTelemetrySummary]
  evidence: bounded list[EvidenceItem]
  warnings: bounded list[safe code/message]
```

A `ProjectComponent` should contain only bounded, already-normalized values: stable component ID, display name, service name when known, container references, state, health, restart count, image identity in the same redacted form already allowed by MC-6.5, resource observation, and association evidence. It must not contain raw inspect payloads, environment variables, mount host paths, network endpoints, credentials, or arbitrary labels.

The public identifier must be opaque and deterministic within the local installation. It may be derived from a normalized project path and a runtime grouping key through a non-reversible stable encoding or digest. The design must not use an unbounded raw path as an API identifier. IDs must be validated server-side and must not permit path traversal or arbitrary filesystem selection.

## 6. Grouping and association strategy

No single signal is sufficient for all installations. The grouping engine should apply an ordered, explainable strategy and retain the evidence used for every association.

| Priority | Signal | Association use | Confidence |
|---:|---|---|---|
| 1 | Explicit safe manual metadata mapping, if a future approved local metadata source exists | Maps a known project/application to known runtime groups | Exact |
| 2 | Docker Compose project label and service labels | Associates containers belonging to one Compose application | Exact or probable, depending on label completeness |
| 3 | Compose service names and locally discovered Compose files | Links runtime service names to a discovered local project | Probable |
| 4 | Existing project path/name and normalized runtime group name | Correlates local project inventory with runtime naming | Inferred |
| 5 | Image repository/name normalization | Provides component hints only; never creates a definitive application by itself | Inferred |
| 6 | No reliable signal | Keeps the component in `ungrouped` or `unknown` | Unknown |

The engine must never assume that a directory name, image name, or container name is a complete application identity. It should produce one logical application per deterministic runtime group only when the evidence is sufficient, and should preserve separate discovered projects when runtime association cannot be proven.

A project may therefore have one of four association outcomes: **matched**, **partially matched**, **runtime-only**, or **discovered-only**. The UI should explain which outcome applies. A runtime-only group is not an error; it is a visible application candidate whose local project/Git posture is unavailable. A discovered-only project can still show Git and Compose evidence while reporting that no matching running containers were found.

### Safe fallback behavior

When Docker is unavailable, project discovery should remain independently usable. When project discovery is unavailable, Docker runtime groups should remain visible as runtime-only groups. When both are unavailable, the route should return an explicit unavailable observation with a safe error code and an empty list rather than inventing a zero-project state. When a container cannot be associated confidently, it belongs to `ungrouped` with `confidence=unknown` and an evidence explanation.

## 7. Evidence-based project health model

Project health is an aggregation of observations, not a claim that the application is functionally correct. Every status must have evidence and freshness metadata. The proposed public statuses are `green`, `yellow`, `red`, and `unknown`, matching the requested user-facing vocabulary while keeping the existing model’s `healthy`, `degraded`, `critical`, and `unknown` semantics mappable at the boundary.

| Status | Meaning | Required evidence pattern |
|---|---|---|
| `green` | All required observed components are running and no observed component is unhealthy or critically stale | At least one fresh runtime observation and no red evidence |
| `yellow` | The project is partially operational or has material warnings | Stopped/restarting component, missing health check, stale evidence, partial association, or degraded telemetry |
| `red` | The observed runtime indicates a critical failure | Required component stopped, unhealthy component, repeated restarts above a bounded threshold, or critical source error |
| `unknown` | There is insufficient trustworthy evidence to assess health | No components, unavailable source, semantic error, unresolved association, or all evidence never sampled |

The aggregation must be deterministic, documented, and conservative. It should not convert “no health check configured” into “healthy”; instead, missing health checks become a warning or unknown condition depending on whether other evidence is sufficient. It should not infer application correctness from a running container alone. Restart counts are evidence of instability, not a diagnosis.

Each health response should include a bounded evidence list with category, severity, source, safe code, observed time, freshness state, and a short safe message. It should also include counts such as total components, running, stopped, unhealthy, restarting, missing-health-check, unavailable, and stale. These are explanatory facts, not remediation recommendations.

## 8. Additive API design

The preferred API surface is additive and GET-only:

```text
GET /api/projects
GET /api/projects/{project_id}
GET /api/projects/{project_id}/containers
GET /api/projects/{project_id}/health
```

`GET /api/projects` returns a bounded inventory of logical applications and discovered projects. It should support bounded `limit`, an optional safe status filter, and an optional search term with strict length and character bounds. The response should include `available`, `status`, `error`, an `observation` object, `projects`, and `truncated`.

`GET /api/projects/{project_id}` returns the selected project/application summary, components, Git posture, Compose posture, runtime association, telemetry summary, and evidence. `GET /api/projects/{project_id}/containers` returns the already-normalized related container summaries and should not expose a second container detail schema. `GET /api/projects/{project_id}/health` returns the health aggregation and evidence independently so the UI can refresh health without rebuilding every detail section.

A representative envelope is:

```json
{
  "available": true,
  "status": "ok",
  "error": null,
  "observation": {
    "transport_ok": true,
    "available": true,
    "state": "fresh",
    "observed_at": "...",
    "age_seconds": 2,
    "max_age_seconds": 60,
    "error": null
  },
  "projects": [],
  "truncated": false
}
```

The exact response models should reuse the shared `Observation` contract and safe error serialization. Semantic source failures must be represented as `error`, not as an unexplained empty result. The route must never return tracebacks, SQL, raw provider data, environment values, secret references, arbitrary host paths, webhook destinations, or unbounded Git output.

No POST, PUT, PATCH, DELETE, action, acknowledgement, refresh-from-remote, or lifecycle endpoint is part of MC-6.6.

## 9. Backend architecture and data flow

The design should add one project/application read façade that composes existing sources rather than adding another provider boundary:

```text
FastAPI GET routes
        |
DashboardProjectApi (bounded read façade)
        |
Project aggregation / association mapper
   |        |          |          |
Project  Git read   Compose    Docker MC-6.5
Service  snapshot    read       observations/telemetry
   |
project telemetry/history and health evidence
```

The façade should accept injected dependencies so tests can use deterministic fakes. It should obtain local project inventory from the existing `ProjectService`, local Git posture from the existing read provider seam, Compose metadata/status through existing read-only provider methods, and runtime state through `DockerObservationService`/`DockerTelemetryService` or the already normalized Docker API contracts. It should use existing telemetry/history repositories in their explicit read-only dashboard mode.

The association engine should be a pure, deterministic component where possible. It should take normalized project and runtime records and return associations plus evidence. This keeps grouping logic testable without Docker, Git, or filesystem calls and prevents the façade from becoming a second collector.

The health aggregator should also be pure over typed observations. It should not call mutation-capable providers, run scripts, query arbitrary paths, or create a worker. All source calls must be bounded, failure-isolated, and mapped to safe domain errors.

## 10. Expected implementation seams

The following files are expected candidates only; this design phase does not modify them.

| Area | Expected future change |
|---|---|
| `src/aipm/models/project.py` or a new `src/aipm/models/project_intelligence.py` | Add typed project/application, component, association, evidence, posture, and health contracts without breaking existing models |
| `src/aipm/services/project/service.py` | Extend local discovery/read helpers only; preserve existing discovery behavior and no-mutation boundary |
| `src/aipm/services/project/association.py` | New pure grouping/association logic, if a separate module keeps responsibilities clear |
| `src/aipm/services/project/health.py` | New pure evidence-based health aggregation, if existing analyzers cannot be composed directly |
| `src/aipm/capabilities/dashboard/project_api.py` | New bounded GET-only Mission Control project façade |
| `src/aipm/mappers/project_detail.py` | New safe output mapper and allow-listed error serialization |
| `src/aipm/dashboard/server.py` | Add only the proposed GET routes and dependency wiring |
| `src/aipm/dashboard/static/index.html` | Replace only the `#/projects` placeholder and add bounded detail interactions |
| `src/aipm/dashboard/static/mission-control-shell.js` | Extend navigation/view hooks only if required; preserve `/static` module imports |
| `src/aipm/dashboard/static/mission-control-scheduler.js` | Add project inventory/detail resources through the shared scheduler only |
| `tests/test_mc66_project_api.py` | Backend contract, bounds, availability, evidence, and mutation-exclusion tests |
| `tests/test_mc66_project_grouping.py` | Pure association and fallback tests |
| `tests/test_mc66_project_health.py` | Deterministic health aggregation tests |
| `tests/test_mc66_frontend.py` | Route, rendering, empty/error/freshness, and static asset contract tests |
| `docs/MC-6.6_DESIGN.md` | This design document only during the current phase |

No database migration, schema, worker, frontend framework, systemd deployment file, Docker runtime file, Cloudflare configuration, or credential configuration is expected for MC-6.6.

## 11. Frontend information architecture and UX

The `#/projects` page should become the application inventory entry point. It should present a compact overview first, then progressively reveal detail. The page should not repeat the full Docker container table by default.

The inventory view should contain project cards with display name, source/association type, health badge, component counts, runtime summary, freshness indicator, and the top evidence/warning. A card should clearly distinguish a discovered local project from a runtime-only Docker group. `unknown` and `unavailable` must include an explanation, not merely a colored badge.

Selecting a card should navigate through the existing hash shell to a stable detail state, such as `#/projects/{project_id}` if the router supports nested hashes, or a selected-detail panel while preserving `#/projects`. The detail view should include:

| Section | Content |
|---|---|
| Identity | Display name, stable ID, source, association confidence |
| Runtime tree | Logical project → services/components → bounded containers |
| Health | Status, evidence, counts, freshness, unavailable sources |
| Git posture | Branch, dirty/conflict state, local tracking summary, or safe unavailable state |
| Compose posture | Known Compose files/services and runtime association, without raw file dumps |
| Resources | Existing normalized resource summaries and links to existing history where available |
| Evidence | Bounded, timestamped evidence items with source and severity |

The page should have explicit empty states for no discovered projects, no matching runtime groups, and no association between local and runtime data. It should have explicit unavailable states for Docker, Git, Compose, or project discovery independently. Search and status filters must be server-bounded and must not cause arbitrary client-side provider queries.

The page must contain no acknowledgement, remediation, deployment, update, restart, Compose control, Git action, or AI-agent action controls. Navigation to Docker detail may reuse the existing read-only route, but it must not create a second data model or route family.

## 12. Polling and Observation semantics

Project inventory should use a slower cadence than fast Docker resource telemetry because grouping, Git posture, and Compose metadata are comparatively slow observations. A proposed starting cadence is 60 seconds for inventory and 30–60 seconds for selected detail/health, subject to the existing scheduler’s resource model and visibility pause behavior.

The frontend must render the shared states consistently:

| State | Project-page behavior |
|---|---|
| `fresh` | Show data and current observation age |
| `stale` | Show last-known data with a visible stale indicator and timestamp |
| `unavailable` | Show source-specific explanation and preserve unaffected sections |
| `never_sampled` | Explain that no observation exists yet; do not show zero-health claims |
| `error` | Show a safe semantic/transport error state without private details |
| `unknown` | Explain that evidence is insufficient to classify the project |

A polling failure must not erase a valid last-known observation unless the shared state contract requires it. There must be one scheduler resource per project endpoint, no timer accumulation during navigation, and no polling while the document is hidden if that is the existing scheduler policy. The backend must remain bounded even if the frontend sends invalid limits or filters.

## 13. Security and read-only boundaries

MC-6.6 inherits all MC-5 and MC-6.1–6.5 invariants:

- Dashboard repositories are explicitly constructed with `read_only=True`.
- SQLite uses `mode=ro` and `PRAGMA query_only=ON`; no initialization, migration, directory creation, WAL mode change, checkpoint, commit, or write transaction is permitted in the dashboard path.
- The service-level filesystem write-denial boundary remains required and fail-closed.
- The dashboard remains loopback-only at `127.0.0.1:8787`.
- No Docker lifecycle method is reachable from the dashboard façade.
- No shell execution or arbitrary project script invocation is permitted.
- No Git fetch, pull, checkout, stash, reset, clean, update, or remote mutation is permitted.
- No credentials, environment values, secret references, authorization headers, webhook destinations, raw provider payloads, or unbounded host paths are exposed.
- Notifications remain disabled and no notification worker/provider is activated.
- No Cloudflare, reverse-proxy, public-ingress, systemd-runtime, or deployment operation is part of this milestone.

Static scans should reject lifecycle verbs and mutation route patterns in the project capability package. Tests should use fake providers whose mutation methods raise immediately, proving that every project GET route remains observation-only.

## 14. SQLite, database, and runtime impact

MC-6.6 must reuse the existing read-only repository boundary and existing telemetry/history storage. It must not add a project database, schema, cache table, worker, or background sampler. Project aggregation is request-scoped over bounded observations and existing cached snapshots.

The dashboard must not invoke project discovery or Git operations in a way that writes metadata, creates directories, updates indexes, or initializes state. If an existing provider has a read/write constructor, the dashboard façade must request its explicit read-only mode or use a dedicated read-only observation method. Any source that cannot guarantee read-only behavior is unavailable to the dashboard until that boundary is proven.

The strongest local regression should construct the real dashboard against a seeded temporary read-only-compatible database with active-WAL data, call all project routes, and verify the main database, WAL, SHM, schema, metadata, and sidecars remain unchanged. Existing MC-5.1.x active-WAL and filesystem-denial tests must remain green.

## 15. Systemd, Docker, Cloudflare, and credentials impact

| Surface | MC-6.6 impact |
|---|---|
| Systemd | None. The existing service template remains documentation/deployment scope; no unit installation or runtime operation |
| Docker runtime | Read-only observation calls only; no start/stop/restart/inspect proxy/control, compose mutation, prune, pull, or network changes |
| Cloudflare/public ingress | None. Dashboard remains loopback-only |
| Credentials/providers | None. No remote Git contact, registry access, provider delivery, or credential lookup |
| SQLite | Existing read-only repositories only; no new schema or database |
| Notifications | Disabled; no worker or channel access |

## 16. Testing strategy

Implementation approval should require focused tests before any commit or deployment preparation.

| Test category | Required proof |
|---|---|
| API contracts | Every proposed route returns bounded envelopes, stable keys, safe errors, correct limits, and GET-only behavior |
| Grouping | Exact Compose label associations, service-name associations, partial matches, image-only hints, duplicate prevention, deterministic ordering, and `ungrouped` fallback |
| Health | Green/yellow/red/unknown decisions are evidence-based; missing health checks, restarts, stopped/unhealthy containers, stale data, and unavailable sources are covered |
| Git posture | Local branch/dirty/conflict/known-tracking snapshots are mapped safely; fetch/pull/checkout/stash/reset/clean/update are unreachable |
| Compose posture | Known metadata is bounded; missing/unavailable Compose data is explicit; raw files and arbitrary paths are not exposed |
| Availability | Docker-only, project-only, Git-only, and all-source failure isolation behaves correctly |
| Freshness | Fresh, stale, unavailable, never-sampled, error, and unknown states render and serialize correctly |
| Bounds | Limits, identifiers, filters, project IDs, evidence counts, and component counts are server-enforced |
| Secret/output safety | Secret-like keys/values, external destinations, raw paths, tracebacks, environment data, and provider payloads never appear |
| Frontend | `#/projects` routing, cards, detail panel, component tree, evidence panel, empty/error states, responsive layout, and `/static` module imports |
| Scheduler | No timer accumulation, visibility pause/resume, correct cadence, and no duplicate requests during navigation |
| Regression | MC-5, MC-6.1, MC-6.2, MC-6.3, MC-6.5, telemetry, history, event, incident, notification, and read-only repository suites |
| Static scope | Python compilation, JavaScript syntax, `git diff --check`, mutation/lifecycle scans, production scope scan, and Gate 2.1 harness byte identity |

The future focused suite should include a real-dashboard temporary SQLite invariant and fake provider tests that fail if any mutation method is touched. It should also prove that project aggregation does not instantiate a second Docker collector or database.

## 17. Deployment and rollback considerations

MC-6.6 should not change deployment topology. Before any future deployment approval, the existing read-only production preflight must be repeated for the exact commit. The dashboard should continue to run as the same loopback-only user-level service, with the same filesystem write-denial and read-only SQLite boundaries. No additional package, daemon, reverse proxy, Cloudflare rule, credential, or public endpoint should be required.

Rollback is a repository/service rollback, not an application action. If the milestone is later deployed and must be reverted, stop and remove or disable only the approved dashboard service according to the separate deployment procedure, restore the previous repository commit or service artifact, verify the loopback listener and read-only invariants, and confirm telemetry, MC-3, notifications, Docker runtime, SQLite, and public ingress are unchanged. No project action, Git reset, Compose command, or Docker mutation may be used as a rollback mechanism by the dashboard.

## 18. Explicit non-goals

MC-6.6 does not include starting, stopping, restarting, recreating, pruning, pulling, building, updating, or deploying containers. It does not include Docker Compose control, Git fetch/pull/checkout/stash/reset/clean/update, arbitrary scripts, remediation, approval workflows, AI-agent actions, notifications activation, authentication, public ingress, logs, systemd observation, or remote registry inspection.

It also does not promise perfect semantic application discovery. A logical platform may require explicit metadata in a future milestone; until then, the design must show confidence and evidence and retain runtime-only or ungrouped records rather than inventing relationships.

## 19. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Compose labels are absent or inconsistent | Preserve runtime-only/ungrouped groups and show confidence/evidence |
| Project directory names do not match runtime names | Use ordered signals; never treat name similarity as exact without evidence |
| A running container does not imply application health | Require evidence-based aggregation and expose missing health checks |
| Git provider accidentally performs remote or write operations | Inject a read-only snapshot seam and add mutation-failing fakes/static scans |
| Slow discovery blocks dashboard requests | Reuse bounded telemetry/cached snapshots and isolate unavailable sources |
| Stale data appears current | Reuse `Observation` and freshness semantics in every source section |
| More detail recreates the container overload problem | Show project cards first and component details on demand |
| Sensitive paths or labels leak through correlation | Use allow-listed mappers, bounded identifiers, and secret/output scans |
| New façade duplicates Docker collection | Reuse MC-6.5 observation/telemetry dependencies and test construction count |
| Partial source failure becomes a false healthy result | Preserve source-specific errors and conservative `unknown` status |

## 20. Implementation sequence after explicit approval

1. Reconfirm the repository baseline and inspect the exact current interfaces without touching runtime state.
2. Freeze typed contracts for project identity, association evidence, posture observations, project health, and safe error envelopes.
3. Implement pure grouping and health aggregation with deterministic fixtures.
4. Add read-only project façade dependency injection over existing project, Git, Compose, Docker, telemetry, and history seams.
5. Add additive bounded GET routes and backend contract tests.
6. Add safe mappers and secret/output/mutation scans.
7. Replace only the `#/projects` placeholder using the existing vanilla shell, `/static` imports, and shared scheduler.
8. Run focused MC-6.6 tests, all MC-5/MC-6 regressions, compilation, syntax, diff, and scope checks.
9. Perform a final review and stop for separate commit approval.
10. After a separate deployment approval, repeat the read-only production preflight; do not begin deployment automatically.

## 21. Stop condition before MC-6.7

MC-6.6 is complete only when project inventory, project detail, health evidence, Git posture, Compose/runtime associations, and the functional `#/projects` page are implemented and validated without changing the read-only architecture. After final review, the agent must stop. MC-6.7 Systemd observation must not begin automatically.

## 22. Current phase result

This document is the only intended MC-6.6 change in the current phase. No source files, tests, configuration, systemd templates, deployment artifacts, databases, Docker runtime, Cloudflare configuration, credentials, providers, telemetry, MC-3 state, or notification state are modified by the design activity.

## References

[1]: ../docs/MC-6_IMPLEMENTATION_PLAN.md "MC-6 implementation plan"
[2]: ../docs/MC-6_API_GAP_ANALYSIS.md "MC-6 API gap analysis"
[3]: ../docs/MC-6.5_DESIGN.md "MC-6.5 Docker intelligence design"
[4]: ../src/aipm/services/project/service.py "Existing project discovery service"
[5]: ../src/aipm/models/project.py "Existing project domain model"
[6]: ../src/aipm/capabilities/dashboard/docker_api.py "MC-6.5 Docker dashboard façade"
[7]: ../src/aipm/dashboard/static/index.html "Mission Control frontend shell and views"
[8]: ../src/aipm/dashboard/static/mission-control-scheduler.js "Shared Mission Control scheduler"
[9]: ../src/aipm/models/mission_control.py "Shared Observation contract"
