# MC-6.13 AI Advisor Status

**Status date:** 2026-08-26
**Repository:** [MenaYassa/AIPM](https://github.com/MenaYassa/AIPM)
**Current commit:** `ead26b68155baee6c38e1f47ad124ae676ea56f7` — `feat: add bounded advisor observability summary`
**Remote parity:** `HEAD == origin/main`; Phase 4E is landed and the supplied production validation passed. This documentation synchronization is intentionally pending its own review/commit.

## Current status

MC-6.13 Phases 2, 3, 4A, 4B, 4C.1, 4C.2, 4C.3, 4D, and 4E are complete, reviewed, committed, and pushed. Phase 4B establishes a private authenticated read-only `POST /api/advisor/evaluate` boundary for bounded caller-supplied evaluation. Phase 4D adds a private-VPS, telemetry-owned bounded snapshot/export and a transport-neutral observation adapter for the approved CPU, memory, and disk slice. Phase 4C.1 adds the server-owned live read-only `GET /api/advisor` orchestration path, and Phase 4C.2 aligns evaluation to a completed telemetry sample boundary without changing the five-minute completeness contract. Phase 4C.3 confirms that complete evidence with no threshold-triggering conditions correctly produces zero findings and recommendations. Phase 4E adds an additive bounded `resource_history_summary` presentation field derived from preserved typed history evidence, while preserving explicit fixture mode. Phase 4B.1 is decided as Cloudflare Access edge-only protection for the documented public hostname; AIPM does not verify Cloudflare JWTs or identity headers, and stronger identity-aware application behavior remains a separate future decision.

### Phase 4C boundary

**CURRENT:** Browser → Cloudflare Access edge → live read-only `GET /api/advisor` presentation on the existing `#/ai-agent` route. The browser makes one bounded request with no polling and supplies no authoritative evaluation timestamp. Explicit fixture mode remains available and is never mixed with live data.

**LIVE BACKEND:** Server-owned orchestration → telemetry owner bounded export → `TelemetrySnapshotExport` → Phase 4D observation adapter → `AdvisorCompositionRequest` → existing Phase 4A boundary → bounded `AdvisorResponse`.

**FIXTURE BACKUP:** The fixture renderer remains a deterministic test/fallback capability with fixed bounded responses and no live collection or evaluation.

**EDGE BOUNDARY:** Cloudflare Access protects the documented public ingress; AIPM relies on that private edge protection and does not implement JWT verification, identity middleware, sessions, or proxy-header trust.

### Phase 4B.1 decision

Cloudflare Access is the selected and confirmed authentication boundary for the documented public ingress. AIPM relies on private edge protection and does not implement JWT verification, identity middleware, session storage, or proxy-header trust. Stronger identity-aware application behavior remains a separate future decision.

**FUTURE/BLOCKED:** Actions, approvals, remediation, LLM/provider integration, and stronger application identity behavior remain separate future work.

### Phase 4C.1 production completeness capture

The supplied read-only production capture confirms that the canonical live telemetry database is `/home/mina/.local/state/aipm/telemetry/mission_control.db`, the deployed telemetry cadence is `60s`, and the advisor requests a `300s` evaluation window. The captured window contains five sample runs spanning `240s`. CPU, memory, and disk each report `insufficient` with reason `insufficient_coverage`, while `invalid_source_rows=0`. This is the expected fail-closed result because the available valid history does not span the complete five-minute window.

The dashboard’s `history 15/15` is explicitly **source coverage, not temporal completeness**: it represents fifteen observed history metric records out of fifteen expected records, not fifteen points per metric or a five-minute span. The three identical `missing_evidence — Resource-history window for agent is incomplete` records are expected, one for each incomplete CPU, memory, and disk history envelope. No Phase 3 completeness semantics or adapter behavior is changed.

### Phase 4E production validation

The deployed Phase 4E commit is `ead26b68155baee6c38e1f47ad124ae676ea56f7`. The operator performed only the authorized `aipm-dashboard.service` restart; no telemetry, database, configuration, source, infrastructure, authentication, Cloudflare, Systemd unit, Docker, nginx, or other runtime modification was reported. The first post-checkout response lacked the additive field because the old dashboard process was still running; the response after restart returned HTTP 200 with `resource_history_summary` present.

The validated live response was `status=fresh`, `available=true`, with `evaluation_time == generated_at` at `2026-08-26T18:02:10+00:00`, zero uncertainties, zero findings, and zero recommendations. The bounded summary appeared in deterministic CPU, memory, disk order and exposed no `maximum_gap` field:

| Metric | State | Valid points | Temporal span | Cadence | Peak | Peak observed at |
|---|---|---:|---:|---:|---:|---|
| CPU | `complete` | 6 | 300s | 60s | 30.2% | `2026-08-26T17:57:10+00:00` |
| Memory | `complete` | 6 | 300s | 60s | 58.8% | `2026-08-26T17:57:10+00:00` |
| Disk | `complete` | 6 | 300s | 60s | 56.4% | `2026-08-26T17:57:10+00:00` |

Complete evidence is an evidence-sufficiency state, not a health claim. It does not itself create a finding or recommendation; the zero result is correct for this complete low-pressure evaluation. Incomplete, stale, unavailable, and invalid evidence semantics remain explicit and fail closed.

The legacy database investigation is closed without inferring inactivity from sandbox evidence:

```text
ACTIVE_CONSUMER=UNKNOWN
PROCESS=UNKNOWN
SERVICE=UNKNOWN
DOCKER_CONSUMER=UNKNOWN
DATABASE_ACTION_REQUIRED=NO
```

The `/home/ubuntu/...` database must not be deleted, modified, or treated as active or inactive without a separately authorized production inspection. The canonical managed writer and dashboard/advisor reader path remains the mina database path.

```text
MC6.13_PHASE2_REVIEW=PASS
MC6.13_PHASE2_COMMIT=ebe1f848947981ee771c4435a19272a50445cb65
MC6.13_PHASE2_PUSH=COMPLETE
MC6.13_PHASE3_REVIEW=PASS
MC6.13_PHASE3_COMMIT=a7ee2f1b90932772fcb7855d9e41a7fa01252824
MC6.13_PHASE3_PUSH=COMPLETE
MC6.13_PHASE4A_REVIEW=PASS
MC6.13_PHASE4A_COMMIT=37d8a0ecca26f82f2a5bcfee54c26bee1e89bd70
MC6.13_PHASE4A_PUSH=COMPLETE
MC6.13_PHASE4B_REVIEW=PASS
MC6.13_PHASE4B_COMMIT=af1a10b1f150335df27fda5d915f44e4f14146f4
MC6.13_PHASE4B_PUSH=COMPLETE
MC6.13_PHASE4D_EXPORT=COMPLETE
MC6.13_PHASE4D_EXPORT_COMMIT=f0ae4bb79dd9370f0d6cc118df49a4d6c4b4b265
MC6.13_PHASE4D_ADAPTER=COMPLETE
MC6.13_PHASE4D_ADAPTER_COMMIT=d90d32f54edc5abf373ecd0308b4963e9a6cabcc
MC6.13_PHASE4C=LANDED_FIXTURE_ONLY
MC6.13_PHASE4C_COMMIT=e8f0b12d7473e3c021c536e738c8b3a414d116ad
MC6.13_PHASE4B1=DECISION_RECORDED_EDGE_ONLY
MC6.13_PHASE4C2=LANDED
MC6.13_PHASE4C2_COMMIT=5e0730cdd46580bfcf6368e8e3216b32772084dd
MC6.13_PHASE4C3=VALIDATED_COMPLETE_EVIDENCE
MC6.13_PHASE4E=LANDED
MC6.13_PHASE4E_COMMIT=ead26b68155baee6c38e1f47ad124ae676ea56f7
MC6.13_PHASE4E_PRODUCTION_VALIDATION=PASS
```

## Phase 2 — evidence normalization

Phase 2 established the isolated normalization seam in:

```text
src/aipm/services/advisor/__init__.py
src/aipm/services/advisor/normalizer.py
tests/test_advisor_normalizer.py
```

The normalizer accepts only bounded mapping-shaped source observations supplied by its caller and converts them into immutable Phase 1 `EvidenceBundle` values. It requires a timezone-aware caller-supplied `evaluation_time`, performs deterministic ordering and serialization, derives freshness without reading the current clock, preserves safe bounded fields for valid degraded states, and emits explicit uncertainty for missing, stale, unavailable, invalid, conflicting, incomplete, or unverified evidence. It performs no filesystem, process, network, provider, Docker, Systemd, database, credential, or provenance-socket access.

Phase 2 validation completed with 18 focused tests and 444 full-suite tests passed, with the existing unrelated Starlette/httpx deprecation warning. The Phase 2 commit is `ebe1f848947981ee771c4435a19272a50445cb65`, subject `feat: establish MC-6.13 evidence normalization seam`.

## Phase 3 — deterministic rule engine

Phase 3 established the pure rule engine in:

```text
src/aipm/services/advisor/__init__.py
src/aipm/services/advisor/rules.py
tests/test_advisor_rules.py
```

The fixed catalog is `mc613-rules-v1`, with ten deterministic rules:

| Rule ID | Category | Purpose |
|---|---|---|
| `service.health.unavailable` | `service_health` | Detect explicit unavailable service-health evidence. |
| `service.health.stale` | `service_health` | Detect explicit stale service-health evidence. |
| `resource.pressure.sustained` | `resource_pressure` | Detect complete, continuous, threshold-sustained resource pressure. |
| `resource.pressure.spike` | `resource_pressure` | Detect an explicit changed resource comparison. |
| `telemetry.cadence.gap` | `telemetry_anomaly` | Detect an explicit cadence gap. |
| `telemetry.source.degraded` | `telemetry_anomaly` | Detect explicit degraded retention/source state. |
| `deployment.revision.changed` | `deployment_change` | Detect an explicit deployment revision comparison change. |
| `deployment.posture.unverified` | `deployment_change` | Detect an unverified runtime confirmation state. |
| `project.state.changed` | `project_state_change` | Detect a change for a proven project identity. |
| `project.health.degraded` | `project_state_change` | Detect explicit degraded project health with supporting evidence. |

### Canonical field schema

Rules consume only the following canonical fields. No aliases are supported and no implicit unit conversion is performed.

| Field | Type or allowed values | Purpose |
|---|---|---|
| `service_status` | string: `healthy`, `degraded`, `critical`, `unavailable`, `unknown` | Service-health status. |
| `metric` | string: `cpu_percent`, `memory_percent`, `disk_percent` | Resource metric identity. |
| `value` | finite number, percent | Resource point value. |
| `unit` | exactly `percent` | Resource unit. |
| `comparison_status` | `unchanged`, `changed`, `missing`, `unavailable`, `indeterminate` | Explicit comparison result. |
| `baseline_value`, `current_value` | finite numbers, percent | Resource comparison sides. |
| `cadence_seconds` | positive finite number, seconds | Telemetry cadence. |
| `retention_status` | `healthy`, `unavailable`, `invalid`, `failed` | Retention/source status. |
| `baseline_revision`, `current_revision`, `revision` | bounded strings | Deployment revision identities. |
| `runtime_confirmation_status` | `observed`, `unavailable`, `not_observed`, `stale`, `invalid` | Runtime confirmation state. |
| `identity_proven` | boolean | Evidence-backed project identity. |
| `changed_field` | `revision`, `branch`, `dirty`, `runtime_association`, `component_count`, `health_state` | Allow-listed project state field. |
| `health_status` | `healthy`, `degraded`, `critical`, `unknown` | Project-health projection. |
| `supporting_evidence_count` | positive integer | Supporting health evidence count. |

The obsolete names `baseline`, `current`, `before_revision`, `after_revision`, `sample_interval_seconds`, `runtime_state`, `state_field`, `health_state`, and `supporting_evidence` are not supported rule inputs.

### Sustained-pressure continuity contract

`resource.pressure.sustained` accepts an immutable bounded `ResourceHistoryEnvelope`. The envelope requires a bounded resource identity; an allow-listed CPU, memory, or disk metric; the exact `percent` unit; a positive finite cadence no greater than 86,400 seconds; timezone-aware ordered window bounds; producer-supplied completeness; at most 128 immutable points; unique evidence IDs; and unique normalized timestamps.

A positive sustained finding requires at least three valid observed points, a requested window of at least five minutes, actual point coverage of at least five minutes, and adjacent gaps no greater than `1.5 × cadence_seconds`, with equality accepted. CPU, memory, and disk thresholds are inclusive at 85%, 85%, and 90%, respectively. Incomplete, sparse, short, malformed, conflicting, stale/degraded, invalid, or mismatched histories fail closed with explicit uncertainty.

Envelope points are not independently authoritative. Each point must bind exactly to its referenced `EvidenceItem` by resource identity, state, canonical metric, canonical value, canonical unit, and UTC-normalized `observed_at`. Any mismatch produces invalid-evidence uncertainty and cannot satisfy continuity or produce a positive finding.

## Phase 3 safety boundary

The Phase 3 engine accepts immutable evidence plus caller-supplied request and evaluation context. It does not read the current clock, generate randomness or UUIDs, access filesystem/process/network/provider state, invoke Docker or Systemd, read credentials, access the provenance socket, or perform autonomous actions. It produces deterministic findings and explanatory, non-executable recommendations while propagating uncertainty explicitly.

The engine does not create an `ActionPlan`, `ApprovalBinding`, executable operation, authoritative audit transition, provider invocation, shell/Systemd/Docker/database operation, or autonomous action. Any future execution, if ever approved, must remain behind the separately governed MC-6.12 control plane.

## Review and validation record

Phase 3 passed strict review after resolving the following historical blockers: generic aliases were rejected in favor of a canonical field schema; timestamp-only sustained-pressure inference was replaced by a bounded history envelope; duplicate timestamps were rejected; and envelope values were bound exactly to normalized evidence values and timestamps. These historical decisions remain documented without weakening the current PASS state.

Final Phase 3 validation:

| Check | Result |
|---|---:|
| Focused Phase 3 suite | 29 passed |
| Full repository suite | 473 passed, 1 existing warning |
| `git diff --check` | PASS |
| Runtime/authority import boundary | PASS |
| Generated-artifact cleanup | PASS |
| Exact committed scope | PASS |
| MC-6.12A / MC-6.12B | Protected |
| Telemetry and provenance runtime | Untouched |
| Dashboard/Systemd/Docker/Cloudflared/database/VPS | Untouched |
| Gate 2.1 harness | Protected |
| Gate 2.1 SHA-256 | `9e12cdc01f901381ff34b16dd68c11a14cf1158e1c32bbde928bce13c6c238e7` |

### Phase 4A validation

| Check | Result |
|---|---:|
| Focused Phase 4A suite | 26 passed |
| Full repository suite | 499 passed, 1 existing Starlette/httpx deprecation warning |
| Exact scope, authority scan, protected-state checks, and artifact cleanup | PASS |

## Phase 4 status

Phase 4A composition is implemented, reviewed, committed, and pushed at `37d8a0e`. Phase 4B is implemented, reviewed, committed, and pushed at `af1a10b` as a private authenticated read-only API transport boundary over Phase 4A, behind the selected Cloudflare Access edge protection. Phase 4D is implemented, reviewed, committed, and pushed in two commits: `f0ae4bb` provides the telemetry-owned bounded snapshot/export and `d90d32f` maps its approved CPU, memory, and disk payload into canonical advisor observations and `ResourceHistoryEnvelope` values, stopping at `AdvisorCompositionRequest`. Phase 4C is implemented, reviewed, committed, and pushed at `e8f0b12` as the explicit fixture-only presentation capability. Phase 4C.1 is implemented, reviewed, committed, and pushed at `6d6bd63` as the live read-only `GET /api/advisor` orchestration and provider path. Phase 4C.2 is implemented, reviewed, committed, and pushed at `5e0730c` as the telemetry-owned completed-sample alignment boundary, and Phase 4E is implemented, reviewed, committed, and pushed at `ead26b6` as the additive bounded resource-history observability summary. The live path uses Cloudflare Access edge protection, a server-owned evaluation context, a bounded five-minute telemetry export, the Phase 4D adapter, and the existing Phase 4A composition boundary; explicit fixture mode remains available and separate. Phase 4B.1 records the selected Cloudflare Access edge-only decision; AIPM relies on private edge protection and does not implement JWT verification, identity middleware, session storage, or proxy-header trust. Actions, approvals, remediation, and LLM/provider integration remain future, separately authorized work.

## Phase 4B — private authenticated advisor evaluation API

Phase 4B adds the transport adapter in `src/aipm/capabilities/advisor/api.py` and wires it into the existing FastAPI application through `src/aipm/dashboard/server.py`. The route is `POST /api/advisor/evaluate`. It requires `request_id`, a caller-supplied timezone-aware `evaluation_time`, bounded `observations`, bounded `expected_sources`, and bounded typed `history_envelopes` input. It rejects unknown fields, reconstructs immutable Phase 3 history objects, invokes `AdvisorCompositionRequest` and `compose_advisor()` directly, and serializes the existing `AdvisorResponse` without semantic rewriting.

Authentication is an injected private dependency and fails closed when unavailable. Rejected authentication returns safe 401; malformed transport input returns safe 400; domain validation returns safe 422; and unexpected internal failures return safe 500. Error responses contain only fixed codes/messages and bounded field descriptors, never raw exceptions, tracebacks, credentials, paths, SQL, commands, or internal topology. The route does not perform actions. The live `GET /api/advisor` path is a separate server-owned orchestration boundary that obtains telemetry only through the Phase 4D export and adapter, uses a completed telemetry sample boundary and bounded five-minute window, and delegates to Phase 4A. The fixture-only Phase 4C capability remains explicit and separate. Phase 4E adds only the bounded additive resource-history summary; stronger application identity behavior remains a separate future decision.

### Phase 4B validation

| Check | Result |
|---|---:|
| Focused Phase 4B suite | 17 passed |
| Full repository suite | 516 passed, 1 existing Starlette/httpx deprecation warning |
| Exact four-file implementation scope | PASS |
| Error-boundary strict review | PASS |
| Runtime/authority scan and protected-state checks | PASS |
| Generated-artifact cleanup | PASS |

## Related documentation

- [`MC-6_STATUS.md`](MC-6_STATUS.md) — current Mission Control ledger.
- [`MC-6_IMPLEMENTATION_PLAN.md`](MC-6_IMPLEMENTATION_PLAN.md) — milestone sequence and gates.
- [`MC-6_ARCHITECTURE.md`](MC-6_ARCHITECTURE.md) — architecture and security boundaries.
- [`MISSION_CONTROL.md`](MISSION_CONTROL.md) — operational dashboard and ingress documentation.
- [`../PROJECT_STATUS.md`](../PROJECT_STATUS.md) — broader project status.
- [`../PRODUCTION_ROADMAP.md`](../PRODUCTION_ROADMAP.md) — broader update-management roadmap.

```text
MC6.13_PHASE4A=COMPLETE
MC6.13_PHASE4A_COMMIT=37d8a0ecca26f82f2a5bcfee54c26bee1e89bd70
MC6.13_PHASE4B=COMPLETE
MC6.13_PHASE4D=COMPLETE
MC6.13_PHASE4D_EXPORT_COMMIT=f0ae4bb79dd9370f0d6cc118df49a4d6c4b4b265
MC6.13_PHASE4D_ADAPTER_COMMIT=d90d32f54edc5abf373ecd0308b4963e9a6cabcc
MC6.13_PHASE4C=LANDED_FIXTURE_ONLY
MC6.13_PHASE4C_COMMIT=e8f0b12d7473e3c021c536e738c8b3a414d116ad
MC6.13_PHASE4C1=LANDED_LIVE_READ_ONLY
MC6.13_PHASE4C1_COMMIT=6d6bd63b59f6117c5f6c1ac087506846b1a11e8a
MC6.13_PHASE4B1=DECISION_RECORDED_EDGE_ONLY
MC6.13_PHASE4C2=LANDED
MC6.13_PHASE4C2_COMMIT=5e0730cdd46580bfcf6368e8e3216b32772084dd
MC6.13_PHASE4C3=VALIDATED_COMPLETE_EVIDENCE
MC6.13_PHASE4E=LANDED
MC6.13_PHASE4E_COMMIT=ead26b68155baee6c38e1f47ad124ae676ea56f7
MC6.13_PHASE4E_PRODUCTION_VALIDATION=PASS
MC6.13_RUNTIME_INTEGRATION=LIVE_ADVISOR_READ_ONLY_ONLY
MC6.13_API=PRIVATE_AUTHENTICATED_GET_AND_POST_READ_ONLY
MC6.13_UI=LIVE_PROVIDER_WITH_EXPLICIT_FIXTURE_MODE
MC6.13_LLM=NOT_IMPLEMENTED
MC6.13_AUTONOMOUS_ACTIONS=NOT_AUTHORIZED
```
