# MC-6.6.1 Design — Project Association & Discovery Refinement

> **Current-state notice — 2026-08-28:** This document is retained as part of the AIPM documentation record. Its historical design or milestone narrative remains valid as historical context, but current completion, publication, deployment, and live-observation claims are superseded by [`docs/CURRENT_STATUS.md`](CURRENT_STATUS.md) and [`docs/LIVE_VPANEL_READONLY_FINDINGS.md`](LIVE_VPANEL_READONLY_FINDINGS.md). The current tracked repository is synchronized at `1c1cc4d8839d122f46eb8a1c7592c9c504df68ba`; MC-6.12 operational execution remains blocked, and the incident-reopen workstream remains preserved separately in `stash@{0}`.


**Status:** Design and investigation only
**Baseline:** `36af5035c51de0f8f272dd912eae170a2113a458`
**Parent milestone:** MC-6.6 Project & Application Intelligence
**Implementation:** Not started
**VPS evidence:** User-provided observation; no VPS access was performed

## 1. Executive summary

MC-6.6.1 addresses a usability gap exposed by the first real VPS observation of the MC-6.6 project inventory. The current implementation is conservative in one sense—it refuses to claim an association when the evidence is not exact—but its primary inventory combines broad filesystem discovery with runtime groups. As a result, the dashboard can show directories such as `.nuget`, `aipm`, and `claude` as projects even when they have no runtime components, while a large runtime group can remain `unknown` when its Docker identity does not exactly match a discovered local project name.

The refinement must not solve this by making name matching more aggressive. The correct design is to separate **runtime application inventory** from **local project discovery**, make exact Compose identity the primary runtime grouping key, narrow or deprioritize broad filesystem discovery, preserve explicit unknown/runtime-only/ungrouped states, and expose the evidence and scope that produced every record.

The target question becomes:

> Which runtime applications are actually present, which components belong to each one, and what trustworthy local project evidence is associated with them?

A separate question remains useful but lower priority:

> Which local Git/Compose project roots were discovered even though no running runtime group is currently associated with them?

Those questions should not be represented as one undifferentiated list.

## 2. Investigation boundary and evidence status

The current repository is inspected at the committed MC-6.6 baseline `36af5035c51de0f8f272dd912eae170a2113a458`. The report also analyzes the VPS behavior supplied in the instruction: 15 discovered projects, several filesystem-oriented names, a 24-component group with `unknown` confidence, and only a subset with exact Docker associations.

No VPS, live database, Docker runtime, systemd service, deployment, credential, Cloudflare, notification, or production configuration was accessed during this design phase. Therefore, the algorithmic causes can be established exactly from repository code, while the exact marker present in each observed VPS directory cannot be independently attributed without a separate approved read-only VPS investigation.

## 3. Current behavior

### 3.1 Filesystem project discovery

`ProjectService.discover()` reads `config.discovery.search_paths`, which defaults to the user home directory when not overridden. It recursively walks each configured path with `os.walk`, applies the configured `ignore_dirs`, respects `max_depth`, and does not follow symlinks unless configured to do so.

A directory becomes a `Project` when either of these conditions is true:

1. It contains a recognized Compose manifest directly: `docker-compose.yml`, `docker-compose.yaml`, `compose.yml`, or `compose.yaml`.
2. It contains a direct `.git` directory.

The current default ignore list excludes common virtual-environment, cache, editor, build, and dependency directories such as `.venv`, `node_modules`, `.cache`, `.local`, `dist`, `build`, and `target`. It does **not** exclude every hidden directory, every package cache, every tool workspace, or every arbitrary Git repository under the home directory.

When a directory qualifies, it is stored as a project and traversal below it stops. Git posture is then attached by `GitProvider.repository(project)` for Git-backed roots. The discovery contract has no concept of “application relevance,” runtime association, trusted project root, or whether a Git repository is merely a tool cache or dependency workspace.

### 3.2 Runtime container normalization

Docker containers are normalized by `DockerMapper.container()`. The mapper extracts the Compose project label `com.docker.compose.project` into the `stack` field, which MC-6.5 maps to `project_key`. It also extracts the Compose service label `com.docker.compose.service` into `service_name` in the Docker detail model.

This is the strongest runtime grouping signal currently available in the dashboard path. It is local Docker metadata and does not require network access, remote registry access, or lifecycle operations.

### 3.3 Current MC-6.6 association algorithm

`ProjectIntelligenceService._matching_group()` compares each discovered project name with each normalized container `project_key`, case-insensitively. It returns a group only when the two values are equal. It does not compare:

- the Compose manifest’s canonical project identity;
- a Compose project label with a local Compose file root;
- explicit application/project labels beyond the standard Compose project label;
- a trusted project-root identity;
- Git repository identity;
- service-name sets;
- a configured project mapping;
- or a stable runtime-to-filesystem association cache.

Consequently, the matching rule is deterministic but narrow. A discovered project with no exact matching runtime group remains a discovered project with `confidence=unknown`. A runtime group that has no exact-matching discovered project becomes a runtime-only application with `confidence=unknown`. Containers with no `project_key` are placed in the explicit `ungrouped` record.

The current implementation also calls `ComposeService.status(project)` only for a discovered project that already has local Compose files. That status call queries Docker containers using the project name label; it does not establish a broader project-root association and does not run Compose lifecycle commands.

## 4. Root-cause analysis of the observed VPS behavior

### 4.1 Why 15 projects are generated

The repository can produce 15 project records because the configured discovery root is broad and the discovery predicate is structural rather than application-aware. Every directory under the configured search path that contains a direct `.git` directory or a recognized Compose file is returned as a project, subject only to the ignore list and depth limit.

Therefore, names such as `.nuget`, `aipm`, and `claude` appear for one of two repository-consistent reasons:

- the directory itself contains a direct `.git` directory; or
- the directory itself contains one of the recognized Compose manifest filenames.

The current code does not inspect whether the directory runs a service, belongs to a trusted application root, is a package cache, or has any Docker association before placing it in the discovered-project list. A filesystem discovery record with zero runtime components is therefore expected behavior under the current contract, not evidence that the directory is a running application.

The exact qualifying marker for each VPS-observed name is not knowable from the sandbox and must not be guessed. The design conclusion is independent of that detail: broad home-directory recursion treats every qualifying local repository or Compose directory as equally important, which is the false-positive source.

### 4.2 Why a 24-component group can remain `unknown`

The current association code assigns `unknown` confidence to a runtime group in either of these situations:

1. The 24 containers share a `project_key`, but no discovered `Project.name` equals that key case-insensitively. The group is then emitted as `source=runtime_group`, with `confidence=unknown` and evidence that no local project matched.
2. The containers do not carry a usable `com.docker.compose.project` label, or the runtime snapshot has no usable project key. The components are emitted as `ungrouped`, and the resulting application cannot receive an exact association.
3. A discovered local project exists, but its directory name differs from the Compose project identity. The current code does not derive or compare a canonical Compose identity from the local manifest root, so the local project remains unmatched even when it may be the correct root.

A 24-component count does not change this result. Component cardinality is evidence that a runtime group is substantial, but it is not proof of its local project identity. The current algorithm intentionally does not promote a large group from `unknown` to `probable` merely because many containers share a name or appear together.

The observed 24-component case should therefore be diagnosed as a **runtime group with strong internal cohesion but insufficient local-root evidence**, unless a future approved inspection proves that its Compose project identity maps to a trusted local root. The design must preserve that distinction.

## 5. EXISTS / EXTEND / NEW / UNAVAILABLE classification

| Capability | Classification | MC-6.6.1 position |
|---|---|---|
| Local Git/Compose directory discovery | **EXISTS** | Reuse as a lower-level local-project source |
| Configured discovery paths, ignore rules, depth, symlink policy | **EXISTS** | Preserve configuration semantics; add safer project relevance policy |
| Docker container state and resource observations | **EXISTS** | Reuse MC-6.5; no second collector |
| Compose project and service labels | **EXISTS** | Promote exact Compose identity to the primary runtime grouping key |
| Compose status via `ComposeService.status()` | **EXISTS** | Reuse only its read-only `status()` method |
| Canonical local Compose project identity | **EXTEND** | Derive only from safe local Compose metadata or an explicit approved mapping; never execute Compose |
| Trusted filesystem project-root classification | **NEW** | Add a pure, bounded relevance classifier over already discovered roots |
| Runtime-first application inventory scope | **NEW** | Separate runtime-backed applications from local-only candidates |
| Explicit application/project metadata mapping | **UNAVAILABLE unless explicitly configured** | Do not infer arbitrary labels or read arbitrary config; a future allow-listed metadata source may be added |
| Remote Git identity or remote verification | **UNAVAILABLE** | No fetch, network access, credential use, or remote comparison |
| Semantic application discovery from image names alone | **UNAVAILABLE** | Image names may be component hints, never definitive project identity |
| Logs, systemd associations, AI-agent interpretation, remediation | **UNAVAILABLE and out of scope** | Defer to later milestones |

## 6. Evidence hierarchy

The refined association engine should use an ordered evidence hierarchy. A lower-ranked signal must not override a higher-ranked contradictory signal.

| Rank | Evidence | Permitted use | Confidence ceiling |
|---:|---|---|---|
| 1 | Exact Docker Compose project identity from `com.docker.compose.project` and a trusted local Compose root or explicit exact mapping | Definitive runtime-to-project association | `exact` |
| 2 | Explicit allow-listed container/project metadata, such as a future `aipm.project.id` label, if the label namespace and value policy are approved | Definitive association only when the value maps to a known stable local ID | `exact` |
| 3 | Canonical Compose service/project metadata from local recognized Compose files combined with matching runtime service-label sets | Strong local/runtime correlation | `probable` or `exact` if identity is canonical and unambiguous |
| 4 | Trusted filesystem project root under an explicitly configured application root, with recognized Compose files and matching runtime metadata | Local-root association and application inventory membership | `probable` |
| 5 | Git repository identity from local state, such as a safe repository-root identity or approved local project ID | Corroborating evidence; never remote verification | `probable` at most |
| 6 | Normalized service-name set or image identity | Component hints and display grouping only | `inferred` at most |
| 7 | Directory name, container name, image substring, or component count alone | Display/search hints only | `unknown`; never an association proof |

The engine must record the winning evidence and rejected or insufficient evidence in bounded evidence objects. It must not expose raw labels, remote URLs, environment values, arbitrary paths, or provider payloads merely to explain a decision.

## 7. Proposed inventory separation

The core correction is to separate three views while preserving the existing endpoint family:

1. **Runtime Applications:** one record per exact or otherwise trustworthy runtime group, including runtime-only groups when no local root is known.
2. **Associated Local Projects:** local Compose/Git roots that have a trustworthy runtime association.
3. **Local Candidates:** discovered local roots with no runtime association, shown separately and lower in priority.

The `#/projects` page should default to Runtime Applications and Associated Local Projects. Local Candidates should be available through a collapsed “Local projects without runtime association” section or an explicit filter. This removes filesystem false positives from the primary application view without deleting the underlying observation.

The API should preserve compatibility by adding a bounded scope parameter rather than removing existing records abruptly:

```text
GET /api/projects?scope=applications
GET /api/projects?scope=associated
GET /api/projects?scope=local
GET /api/projects?scope=all
```

The default behavior should be chosen deliberately during implementation. The safest compatibility path is to keep `scope=all` as the legacy-compatible backend default while making the MC-6.6.1 frontend request `scope=applications`. A later milestone may change the default only after clients and operators have reviewed the contract. The response should add explicit fields such as `inventory_scope`, `association_role`, `source`, `confidence`, and `evidence`; existing fields should not be silently reinterpreted.

## 8. Proposed deterministic association algorithm

### Step 1: Collect normalized runtime groups

Reuse the existing MC-6.5 Docker telemetry/observation path. Group containers first by the exact normalized Compose project label. Preserve the original bounded project key internally and expose only its safe normalized representation. Containers without a usable project key remain in `ungrouped` and are never forced into a named application.

### Step 2: Build a bounded local-root index

Reuse `ProjectService.discover()` as the source of local roots, but classify each root before it enters the primary application inventory. The index should record:

- whether the root has a recognized Compose manifest;
- whether it has a direct Git root;
- whether it is beneath an explicitly configured application root;
- a stable internal root ID;
- safe Compose service names if available through an approved read-only parser or existing model;
- and whether the root is excluded as a hidden/cache/dependency/tool directory.

The public response must not expose arbitrary full paths. A local root may be represented by a safe display name and stable opaque ID, with path detail remaining allow-listed and bounded if the existing contract requires it.

### Step 3: Apply exact Compose identity first

For each runtime Compose project key, look for a local root whose canonical Compose identity is exactly equal to the runtime key or whose explicit approved mapping says they are identical. This is the only route to `exact` confidence.

The canonical identity must not be guessed from an arbitrary directory name. It should come from an existing safe Compose identity field, an explicitly configured project name, or a deterministic local metadata rule that does not execute Compose or access remote state. If canonical identity cannot be obtained safely, the association remains unresolved.

### Step 4: Apply explicit metadata only from an allow-list

If a future implementation supports explicit container labels, it must use a fixed label allow-list and validate the value against known stable local project IDs. It must not treat every arbitrary Docker label as application metadata. No environment, secret, destination, or raw label dump is allowed.

### Step 5: Corroborate with service sets and trusted roots

A local Compose root may receive `probable` confidence when its recognized service-name set matches the runtime group’s allow-listed Compose service labels and the root passes the trusted-root policy. Service names alone must not produce an association because unrelated projects can reuse names such as `db`, `web`, or `api`.

### Step 6: Use Git only as corroboration

Local Git posture may support a project detail view and may corroborate a trusted root, but Git repository existence or directory name alone must not associate a runtime group. No remote fetch, pull, checkout, stash, reset, clean, update, or arbitrary Git command is permitted.

### Step 7: Preserve unresolved states

If no rule reaches the required evidence threshold, emit a runtime-only or ungrouped record with `confidence=unknown` and explicit evidence. The algorithm must not promote a group solely because it has many components, a familiar image name, a similar directory name, or a shared service name.

## 9. False-positive filesystem discovery handling

The current discovery source should not be deleted because it remains useful for local Git/Compose posture. It should be narrowed and deprioritized for application inventory.

### 9.1 Narrowing policy

The future classifier should treat hidden directories and known dependency/tool/cache roots as non-application candidates by default, even if they contain a direct `.git` directory or Compose filename. This should be an explicit policy with an allow-list override for a real application that intentionally uses a hidden root.

The policy should also distinguish configured application roots from a broad home-directory search. A broad home search may remain available for backward compatibility, but its results should be classified as local candidates unless they have trustworthy runtime association or reside under an explicitly configured application root.

### 9.2 Deprioritization policy

A Git-only directory with no Compose metadata and no runtime association should not appear as a first-class application card. It should appear under Local Candidates with a clear explanation: “Local Git project discovered; no runtime association observed.”

A Compose root with no running containers may remain visible as discovered-only if it is an explicitly trusted application root. Otherwise, it should remain available through the local scope rather than the runtime application scope.

### 9.3 Grouping policy

Do not merge filesystem candidates merely because their names are similar. Do not merge `.nuget`, `aipm`, `claude`, or other roots into one application without an explicit shared runtime or approved metadata relationship. Grouping is evidence-driven, not directory-driven.

## 10. Diagnosis of the 24-component group

The 24-component group should be presented as a **cohesive runtime group with unresolved local-root association** until exact Compose identity evidence is available. The detail view should show:

- the runtime group key in safe normalized form;
- component count and service-name distribution;
- exact Compose labels used for grouping, without raw unbounded label dumps;
- whether a trusted local Compose root matched;
- whether any local candidate had a partial service-set match;
- and why confidence remained `unknown`.

If the group’s 24 components share one exact `com.docker.compose.project` value, the runtime grouping itself is strong. The missing piece is local-root identity, not component cohesion. The UI should avoid making the operator interpret `unknown` as “the runtime is unknown”; it should say “runtime group observed; local application root not proven.”

If the group has no exact Compose project label, it should instead remain `ungrouped` or be split only by other exact allow-listed metadata. A large count must not substitute for an identity signal.

## 11. API compatibility implications

The existing routes remain additive and GET-only:

```text
GET /api/projects
GET /api/projects/{project_id}
GET /api/projects/{project_id}/containers
GET /api/projects/{project_id}/health
```

MC-6.6.1 should add bounded parameters such as `scope`, `association`, and a strictly allow-listed `confidence` filter. Existing `limit`, `search`, and `status` bounds must remain enforced server-side.

The response should add, not remove, fields:

```text
inventory_scope
association_role: application | associated_local | local_candidate | runtime_only | ungrouped
association_evidence
association_explanation
runtime_group
local_project_id
```

The existing `source`, `confidence`, `health`, `freshness`, `components`, `git`, `compose`, and safe error envelopes must remain compatible. `scope=all` can preserve the current broad inventory while `scope=applications` provides the refined primary view. Unknown, unavailable, never-sampled, stale, and semantic-error states must continue to use the shared Observation contract.

## 12. Frontend implications

The `#/projects` page should change its default presentation from a flat list of all discovered roots to a runtime-first inventory:

1. Application cards for exact/probable associated runtime groups.
2. Runtime-only cards for cohesive groups whose local root is not proven.
3. An explicit “Unassociated runtime” card for containers without trustworthy project identity.
4. A collapsed Local Candidates section for Git/Compose roots without runtime association.

Each card should show `association_role`, confidence, evidence summary, component count, runtime status, health, and freshness. A filesystem candidate such as `.nuget` should not visually compete with a running platform. The detail view should explain whether the record is runtime-backed, local-only, runtime-only, or ungrouped.

The frontend should request the refined scope explicitly and continue using the existing centralized scheduler. No new timer, framework, build pipeline, action control, or direct Docker/Compose request should be introduced. The existing `/static` module convention remains mandatory.

## 13. Safety analysis

MC-6.6.1 preserves the existing read-only architecture:

- Docker data comes through existing observation/telemetry methods only.
- Compose data comes through existing `ComposeService.status()` read behavior only; `up`, `down`, `pull`, and other lifecycle methods remain unreachable.
- Git data is local snapshot posture only; no fetch, pull, checkout, stash, reset, clean, update, or remote contact is permitted.
- Filesystem inspection remains bounded by configured roots, depth, ignore policy, and allow-listed metadata; arbitrary traversal is not added.
- No shell execution, subprocess invocation, `docker exec`, arbitrary project script, credential lookup, environment-value access, raw provider payload, or public-ingress change is permitted.
- SQLite remains `read_only=True`, `mode=ro`, and `PRAGMA query_only=ON`; no schema, database, worker, sampler, or migration change is required.
- Loopback binding, service hardening, notification-disabled posture, systemd template, Docker runtime, Cloudflare, and deployment topology remain untouched.

Every association decision must have bounded evidence and a safe explanation. Failure to collect one source must remain a source-specific unavailable state rather than a false healthy or exact association.

## 14. Test strategy

The implementation phase should add deterministic fixtures that reproduce the reported classes without accessing the VPS.

| Test area | Required proof |
|---|---|
| Broad discovery | A home-like fixture containing hidden/tool/cache/Git directories proves they become local candidates, not primary applications |
| Trusted roots | Explicit application-root allow-list proves only trusted local roots enter the associated application view |
| Exact Compose identity | Matching runtime project label and canonical local Compose identity yields `exact` |
| Identity mismatch | Similar directory names without exact identity remain unresolved |
| Explicit metadata | Only allow-listed labels map to stable project IDs; arbitrary labels do not |
| Service corroboration | Matching service sets can raise confidence only when a trusted root already exists |
| Image/name fallback | Image or directory similarity alone never creates an association |
| 24-component group | A large exact runtime group without local-root evidence remains runtime-only and `unknown` with an explanation |
| Ungrouped containers | Missing project labels remain explicitly ungrouped |
| False positives | `.nuget`, tool workspaces, and hidden cache roots are excluded or deprioritized according to policy |
| Scope compatibility | `applications`, `associated`, `local`, and `all` are bounded and stable |
| Health | Running alone is not green; missing health checks are warning evidence; source failures remain isolated |
| Observation | Fresh, stale, unavailable, never-sampled, error, and unknown states remain correct |
| API safety | All routes are GET-only; identifiers, limits, search, scope, and filters are bounded |
| Output safety | No secrets, credentials, environment values, raw labels, arbitrary paths, tracebacks, or unbounded output appear |
| Frontend | Runtime-first cards, local-candidate separation, detail explanation, and scheduler behavior are covered |
| Regression | MC-5 through MC-6.6 tests, full pytest, compilation, JavaScript syntax, mutation scans, and harness identity remain green |

## 15. Migration and rollback strategy

No database migration, schema change, worker, or telemetry migration is required. The change is a response-contract and classification refinement over existing observations.

The safest rollout is additive:

1. Add classification fields and `scope` support.
2. Keep `scope=all` available for compatibility.
3. Update the frontend to request `scope=applications` explicitly.
4. Compare runtime application counts and local-candidate counts in a staging review.
5. Only after operator review consider changing any backend default.

Rollback is a repository/service rollback to the previous MC-6.6 commit. It must not use Git mutation through Mission Control, Docker lifecycle operations, Compose commands, or filesystem cleanup. No live database restoration is needed because the design introduces no database writes or schema changes.

## 16. Exact future file changes

The implementation phase should be limited to the following likely paths:

| File | Expected change |
|---|---|
| `src/aipm/services/project/service.py` | Add bounded project-root relevance classification or a separate read-only discovery view; preserve existing discovery behavior for other consumers |
| `src/aipm/services/project/intelligence.py` | Add runtime-first grouping, exact Compose identity, trusted-root correlation, scope separation, and evidence codes |
| `src/aipm/models/project_intelligence.py` | Add association role, scope, evidence, and explanation fields while preserving existing contracts |
| `src/aipm/capabilities/dashboard/project_api.py` | Add bounded scope/association filters and additive response fields |
| `src/aipm/mappers/project_intelligence.py` | Map new safe classification/evidence fields and redact paths/labels |
| `src/aipm/dashboard/static/mission-control-projects.js` | Render runtime-first applications and a separate local-candidate section |
| `src/aipm/dashboard/static/index.html` | Adjust only the existing Projects view markers if required |
| `tests/test_mc661_project_association.py` | Add discovery, grouping, scope, false-positive, and 24-component fixtures |
| `tests/test_mc66_project_intelligence.py` | Extend compatibility and health/observation coverage where necessary |
| `tests/test_mc62_shell.py` | Update only stale shell expectations if the existing Projects markers change |
| `docs/MC-6.6.1_DESIGN.md` | Preserve this design and later record implementation decisions |

No `ops/systemd`, `ops/staging`, database, Docker runtime, Cloudflare, credential, notification, or MC-6.7 file should change.

## 17. Explicit non-goals

MC-6.6.1 does not include Docker lifecycle operations, `docker exec`, Compose control, Git mutation, remote fetch, shell execution, arbitrary filesystem traversal, arbitrary project scripts, credentials, environment values, public ingress, database/schema changes, new collectors, workers, samplers, systemd changes, Cloudflare changes, notification activation, logs, systemd observation, authentication, AI-agent actions, remediation, or MC-6.7 work.

It does not attempt to infer application identity from directory names, image names, container names, service names alone, component count, or semantic guesses about familiar software. It does not delete or hide local discovery data; it separates lower-confidence local candidates from the primary runtime application view.

## 18. Implementation sequence and stop condition

After a separate implementation approval, the sequence should be:

1. Freeze the new scope and association-role contracts.
2. Add pure trusted-root and evidence classification logic.
3. Add exact Compose identity correlation using existing read-only metadata.
4. Add runtime-first/local-candidate API scopes and compatibility fields.
5. Add deterministic fixtures for false positives, exact matches, mismatches, runtime-only groups, ungrouped containers, and the 24-component case.
6. Update only the existing Projects frontend to make runtime applications primary.
7. Run focused, regression, full-suite, compilation, JavaScript, safety, scope, and harness-identity checks.
8. Perform a final review and stop for separate commit approval.
9. If later deployed, use a separate explicit deployment approval and the established read-only preflight.

MC-6.6.1 is complete only when the dashboard’s primary Projects view is useful for runtime applications without inventing relationships, while local-only discovery remains explainable and accessible. After that review, stop. **Do not start MC-6.7 automatically.**

## References

[1]: ../src/aipm/services/project/service.py "Current ProjectService discovery boundary"
[2]: ../src/aipm/services/project/intelligence.py "Current MC-6.6 project association service"
[3]: ../src/aipm/models/config.py "Discovery configuration defaults and limits"
[4]: ../src/aipm/mappers/docker.py "Current Docker Compose label normalization"
[5]: ../src/aipm/services/compose/service.py "Existing read-only Compose status seam"
[6]: ../src/aipm/capabilities/dashboard/project_api.py "Current MC-6.6 project dashboard façade"
[7]: ../docs/MC-6.6_DESIGN.md "MC-6.6 Project & Application Intelligence design"
