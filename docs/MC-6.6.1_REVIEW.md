# MC-6.6.1 Final Review — Project Association & Discovery Refinement

**Review status:** PASS
**Baseline:** `36af5035c51de0f8f272dd912eae170a2113a458`
**Implementation commit:** MC-6.6.1 changes are present in the working tree and are not committed in this review phase
**VPS access:** Not performed

## 1. Review verdict

> **MC6.6.1_REVIEW=PASS**

MC-6.6.1 materially solves the reported discovery problem without weakening the association rules. Filesystem-backed Git/Compose roots that lack trustworthy runtime evidence are now represented as `local_candidate` records and are excluded from the Projects page’s primary `scope=applications` request. Runtime-only Docker groups and ungrouped containers remain visible with explicit unresolved roles and evidence rather than being falsely mapped to local directories.

The implementation is correctly conservative. It improves usefulness by changing inventory scope and presentation, not by inventing relationships from directory names, image names, container names, service names, or component counts.

## 2. Separation model verification

The final domain model provides the required separation:

| Category | Representation | Primary application view |
|---|---|---:|
| Runtime-backed local application | `association_role=associated_local` | Yes |
| Runtime-only Docker group | `association_role=runtime_only` | Yes, visibly unresolved |
| Ungrouped container set | `association_role=ungrouped` | Yes, visibly unresolved |
| Local Git/Compose root without runtime proof | `association_role=local_candidate` | No; returned in `local_candidates` |
| Legacy-compatible combined inventory | `scope=all` | Available through the API |

A directory such as `.nuget`, `aipm`, `claude`, `EAG`, or `invoicing` can still be discovered by the lower-level local discovery source when it satisfies the existing structural Git/Compose predicate. However, if it has no trustworthy runtime association, it is assigned `local_candidate` and is not included in the frontend’s `scope=applications` project cards. This is the required correction: the source observation is retained, but it is no longer presented as a runtime application.

## 3. Association algorithm review

Directory name matching alone cannot create an association. The association service now requires a recognized Compose root and compares the runtime Docker Compose project identity against a bounded canonical identity derived from the recognized Compose metadata. A top-level bounded `name:` value is accepted when safe; otherwise the Compose-root directory name is used only as the Compose identity fallback for a recognized Compose root, not for a Git-only directory.

This means a Git-only directory named similarly to a runtime group cannot become associated merely because of its name. A Compose root with a mismatching canonical identity remains local-only while the Docker group remains runtime-only. Exact associations carry `EXACT_COMPOSE_PROJECT_IDENTITY` evidence. Unresolved runtime groups carry `RUNTIME_ONLY_GROUP` evidence, and ungrouped containers carry `UNGROUPED_RUNTIME` evidence.

The implementation preserves `unknown` confidence whenever the available evidence is insufficient. It does not promote a group because it has many components or because its image/service names look familiar.

## 4. Expected VPS outcome

The reported VPS stacks—Supabase, n8n, Crawl4AI, Cloudflared, OpenVPN, Product Tracker, Color Mixer, Short Video Maker, OmniRoute, LocalAI, and AIPM Mission Control—should be represented according to their actual local evidence rather than by name-based optimism.

A stack whose local Compose root is discovered and whose canonical Compose identity matches the Docker `com.docker.compose.project` identity should appear as an associated runtime-backed application with exact confidence. A stack whose Compose runtime exists but whose local root is not discovered, whose project name was overridden, or whose Compose identity cannot be safely proven should appear as a runtime-only group. Standalone containers without trustworthy project metadata should remain ungrouped. Local repositories or Compose roots with no runtime proof should remain local candidates.

The sandbox cannot verify the actual VPS root paths, Compose `name:` values, project-name overrides, or container labels, and no claim is made that every named stack will receive an exact association. This is intentional. Correctness requires the dashboard to show unresolved evidence instead of incorrectly mapping a stack to an unrelated directory.

## 5. Frontend presentation review

The Projects page now separates the primary runtime-backed inventory from a dedicated **Local candidates** section. The primary request is explicitly:

```text
/api/projects?scope=applications&limit=200
```

Local candidates are populated from the response’s bounded `local_candidates` field. Cards display the association role, confidence, health, freshness, and explanation. Runtime-only and ungrouped records remain visible in the primary runtime inventory but are labeled through their explicit association roles and explanations, so they are not silently presented as proven local applications.

There is one remaining **non-blocking presentation gap**: the current HTML uses the generic heading “Project inventory” for the primary runtime section rather than three separate headings named “Applications,” “Local Projects,” and “Runtime Groups.” The data model and cards already distinguish these categories, and the local-candidate section is explicit. The remaining refinement is a UX taxonomy improvement, not an association correctness or safety defect. It should be addressed in a later review-approved frontend-only refinement if operators require three visually separate sections.

## 6. Remaining gaps and risks

The exact VPS result depends on local Compose identity details that are unavailable in this sandbox. Compose projects launched with an explicit `-p` override, `COMPOSE_PROJECT_NAME`, or other external naming convention may correctly remain runtime-only until an approved local mapping exists. This is preferable to a false association.

The current lower-level filesystem discovery still performs bounded recursive discovery under configured search paths. MC-6.6.1 solves the user-visible false-positive problem through classification and scope separation rather than removing that source. A future policy refinement may narrow configured application roots or add explicit ignore/relevance policy, but such a change should remain separately reviewed and should not turn directory names into identity evidence.

## 7. Validation basis

The implementation review was grounded in the final source, model, mapper, API, frontend, tests, and approved MC-6.6.1 design document. The implementation validation already recorded:

- 15 focused MC-6.6.1/MC-6.2 tests passed;
- 81 explicit MC-5 through MC-6.6 regression tests passed;
- 183 full repository tests passed;
- Python compilation passed;
- JavaScript syntax checks passed;
- diff, mutation/lifecycle, frontend-action, secret/output, and production-scope checks passed;
- the preserved Gate 2.1 harness remained byte-identical.

The existing test warning is the unrelated Starlette/httpx deprecation warning.

## 8. Safety and stop condition

No source implementation was modified during this review. No runtime, VPS, database, systemd, Docker, Cloudflare, credential, notification, telemetry, MC-3, public-ingress, commit, push, or deployment operation was performed. MC-6.7 remains unstarted.

The review is complete with `MC6.6.1_REVIEW=PASS`. Stop before any commit, push, VPS validation, deployment, or MC-6.7 work.

## References

[1]: ../src/aipm/services/project/intelligence.py "MC-6.6.1 association and inventory scope implementation"
[2]: ../src/aipm/models/project_intelligence.py "MC-6.6.1 project intelligence contracts"
[3]: ../src/aipm/capabilities/dashboard/project_api.py "MC-6.6.1 bounded GET-only project API"
[4]: ../src/aipm/dashboard/static/mission-control-projects.js "MC-6.6.1 Projects frontend controller"
[5]: ../tests/test_mc66_project_intelligence.py "MC-6.6.1 focused tests"
[6]: ./MC-6.6.1_DESIGN.md "Approved MC-6.6.1 design"
