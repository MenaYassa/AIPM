"""MC-6.15-A.4: Dashboard Compose Service Candidate Intelligence Frontend Contract.

Verifies the read-only surface in `src/aipm/dashboard/static/mission-control-projects.js`
and `src/aipm/dashboard/static/index.html`:
- Exact backend enum vocabulary: current, update_available, not_applicable, drift, unknown.
- Summary display directly reflects backend counts (declared, running, current, update available,
  not applicable, unknown, drift).
- Minimum required table columns: Service, State, Health, Declared Image, Running Digest,
  Candidate Digest, Candidate Status, Candidate Reason, Dependencies, Freshness, Provenance.
- Separate running and candidate digests side-by-side.
- Local build and disabled profile handling.
- Unknown / network budget exhaustion handling.
- Bounded user-readable error states without raw error/JSON dumps.
- Read-only refresh mechanism without execution or mutation triggers.
- Ban on direct registry querying, executor IPC, and mutation affordances.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PROJECTS_SOURCE = "src/aipm/dashboard/static/mission-control-projects.js"
INDEX_SOURCE = "src/aipm/dashboard/static/index.html"

BACKEND_ENUM_VALUES = (
    "current",
    "update_available",
    "not_applicable",
    "drift",
    "unknown",
)


def test_1_static_exact_status_enum_values_preserved():
    source = Path(PROJECTS_SOURCE).read_text(encoding="utf-8")
    assert "candidateStatusBadge" in source
    for enum_val in BACKEND_ENUM_VALUES:
        assert f"'{enum_val}'" in source or f'"{enum_val}"' in source, (
            f"Missing backend enum {enum_val}"
        )


def test_2_static_table_columns_present():
    source = Path(PROJECTS_SOURCE).read_text(encoding="utf-8")
    table_headers = (
        "<th>Service</th>",
        "<th>State</th>",
        "<th>Health</th>",
        "<th>Declared Image</th>",
        "<th>Running Digest</th>",
        "<th>Candidate Digest</th>",
        "<th>Candidate Status</th>",
        "<th>Candidate Reason</th>",
        "<th>Dependencies</th>",
        "<th>Freshness</th>",
        "<th>Provenance</th>",
    )
    for header in table_headers:
        assert header in source, f"Missing table column header: {header}"


def test_3_static_summary_fields_derive_directly_from_api_counts():
    source = Path(PROJECTS_SOURCE).read_text(encoding="utf-8")
    summary_strip = source.split("compose-summary-strip", 1)[1].split("</div>", 1)[0]
    expected_count_derivations = (
        "p.total_services_count",
        "p.running_services_count",
        "p.current_count",
        "p.updates_available_count",
        "p.not_applicable_count",
        "p.unknown_count",
        "p.drift_count",
    )
    for field in expected_count_derivations:
        assert field in summary_strip, f"Missing direct count derivation for {field}"

    expected_labels = (
        "declared",
        "running",
        "current",
        "update available",
        "not applicable",
        "unknown",
        "drift",
    )
    for label in expected_labels:
        assert label in summary_strip, f"Missing summary label {label}"


def test_4_static_visual_distinctions_defined_in_index_css():
    html = Path(INDEX_SOURCE).read_text(encoding="utf-8")
    for css_cls in (
        ".compose-intelligence-section",
        ".compose-summary-strip",
        ".compose-table-wrap",
        ".compose-table",
        ".badge.status-current",
        ".badge.status-update_available",
        ".badge.status-drift",
        ".badge.status-not_applicable",
        ".badge.status-unknown",
        ".provenance-verified",
        ".provenance-unverified",
    ):
        assert css_cls in html, f"Missing CSS class definition {css_cls}"


def test_5_static_service_intelligence_offers_no_mutation_affordance():
    source = Path(PROJECTS_SOURCE).read_text(encoding="utf-8")
    section = source.split("const composeIntelligenceSection", 1)[1].split(
        "const updateStatusSection", 1
    )[0]
    for banned in (
        "/update/approve",
        "/update/execute",
        "method:'POST'",
        "method: 'POST'",
        "method:'PUT'",
        "method: 'PUT'",
        "method:'DELETE'",
        "method: 'DELETE'",
        "method:'PATCH'",
        "method: 'PATCH'",
        "<form",
    ):
        assert banned not in section, f"Forbidden mutation pattern found: {banned}"


def test_6_static_no_forbidden_control_plane_or_executor_words():
    banned_words = (
        "fencing_token",
        "contract_digest",
        "requester_subject",
        "snapshot_id",
        "evidence_reference",
        "/socket",
        "mutation_receipt",
        "kill-switch",
    )
    for source_path in (PROJECTS_SOURCE, INDEX_SOURCE):
        source = Path(source_path).read_text(encoding="utf-8")
        for banned in banned_words:
            assert banned not in source, (source_path, banned)


def test_7_static_no_frontend_registry_queries():
    source = Path(PROJECTS_SOURCE).read_text(encoding="utf-8")
    # Frontend must not query external registries directly
    for banned in (
        "https://registry-1.docker.io",
        "https://ghcr.io",
        "https://auth.docker.io",
        "docker.io/v2",
        "ghcr.io/v2",
    ):
        assert banned not in source, f"Direct registry URL in frontend: {banned}"


def test_8_static_interpolation_escaped():
    source = Path(PROJECTS_SOURCE).read_text(encoding="utf-8")
    section = source.split("const candidateStatusBadge", 1)[1].split(
        "function bindComposeRefresh", 1
    )[0]
    for fragment in (
        "escapeHtml(svc.service_name)",
        "escapeHtml(raw)",
        "escapeHtml(svc.candidate_reason",
        "escapeHtml(svc.freshness",
        "escapeHtml(p.compose_identity",
    ):
        assert fragment in section, f"Missing escaping for fragment: {fragment}"


def test_9_node_runtime_baseline_render():
    """Execute mission-control-projects.js in Node.js to verify DOM output for live baseline."""
    mock_payload = {
        "available": True,
        "status": "ok",
        "project": {
            "id": "f55f34521cf1e43173e51795",
            "display_name": "local-ai-packaged",
            "compose_identity": "localai",
            "total_services_count": 25,
            "running_services_count": 21,
            "current_count": 12,
            "updates_available_count": 4,
            "not_applicable_count": 7,
            "unknown_count": 2,
            "drift_count": 0,
            "freshness": "fresh",
            "services": [
                {
                    "service_name": "auth",
                    "state": "running",
                    "health": "healthy",
                    "declared_image": "supabase/gotrue:v2.189.0",
                    "running_repo_digests": [
                        "supabase/gotrue@sha256:385184459f57569c54c25209f51f3b2be99ddd7c4ce9e3555b5d3eea8447b7cf"
                    ],
                    "candidate_digest": "sha256:385184459f57569c54c25209f51f3b2be99ddd7c4ce9e3555b5d3eea8447b7cf",
                    "candidate_child_digest": "sha256:cce6c1d00352f22ee7634066012830f69636c157017ea166634c48dbb58eefb7",
                    "candidate_status": "current",
                    "candidate_reason": "up_to_date",
                    "candidate_detail": "Running digest matches registry candidate (multi-arch (arm64))",
                    "is_build": False,
                    "depends_on": ["db"],
                    "freshness": "fresh",
                    "provenance_verified": True,
                },
                {
                    "service_name": "ollama",
                    "state": "running",
                    "health": None,
                    "declared_image": "ollama/ollama:latest",
                    "running_repo_digests": [
                        "ollama/ollama@sha256:1111111111111111111111111111111111111111111111111111111111111111"
                    ],
                    "candidate_digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
                    "candidate_child_digest": None,
                    "candidate_status": "update_available",
                    "candidate_reason": "candidate_digest_differs",
                    "candidate_detail": "Registry candidate digest differs from running image",
                    "is_build": False,
                    "depends_on": [],
                    "freshness": "fresh",
                    "provenance_verified": True,
                },
                {
                    "service_name": "app-build",
                    "state": "running",
                    "health": "healthy",
                    "declared_image": None,
                    "is_build": True,
                    "running_repo_digests": ["app-build:local"],
                    "candidate_digest": None,
                    "candidate_child_digest": None,
                    "candidate_status": "not_applicable",
                    "candidate_reason": "local_build",
                    "candidate_detail": "Service builds from local context; no remote registry candidate applies",
                    "depends_on": ["auth"],
                    "freshness": "fresh",
                    "provenance_verified": True,
                },
                {
                    "service_name": "worker-profile",
                    "state": "not_created",
                    "health": None,
                    "declared_image": "redis:7-alpine",
                    "is_build": False,
                    "running_repo_digests": [],
                    "candidate_digest": None,
                    "candidate_child_digest": None,
                    "candidate_status": "not_applicable",
                    "candidate_reason": "disabled_by_profile",
                    "candidate_detail": "Service is excluded by active Compose profile",
                    "depends_on": [],
                    "freshness": "fresh",
                    "provenance_verified": False,
                },
                {
                    "service_name": "litellm",
                    "state": "running",
                    "health": "healthy",
                    "declared_image": "ghcr.io/berriai/litellm:main-latest",
                    "running_repo_digests": [
                        "ghcr.io/berriai/litellm@sha256:3333333333333333333333333333333333333333333333333333333333333333"
                    ],
                    "candidate_digest": None,
                    "candidate_child_digest": None,
                    "candidate_status": "unknown",
                    "candidate_reason": "network_budget_exhausted",
                    "candidate_detail": "Remote candidate discovery reached max network operation budget (50 ops)",
                    "is_build": False,
                    "depends_on": ["db"],
                    "freshness": "fresh",
                    "provenance_verified": True,
                },
            ],
        },
    }

    script = f"""
global.window = global;
import('./src/aipm/dashboard/static/mission-control-projects.js').then(async m => {{
  const elements = {{}};
  global.document = {{
    getElementById: (id) => {{
      if (!elements[id]) elements[id] = {{ textContent: '', className: '', innerHTML: '', style: {{}} }};
      return elements[id];
    }},
    querySelectorAll: () => []
  }};
  global.fetch = async (url) => {{
    if (url.includes('/compose-intelligence')) {{
      return {{ ok: true, status: 200, json: async () => ({json.dumps(mock_payload)}) }};
    }}
    if (url.endsWith('/health')) return {{ ok: true, status: 200, json: async () => ({{ health: {{ status: 'healthy', counts: {{ running: 21, healthy: 18 }} }} }}) }};
    if (url.endsWith('/containers')) return {{ ok: true, status: 200, json: async () => ({{ containers: [] }}) }};
    if (url.endsWith('/update/status')) return {{ ok: true, status: 200, json: async () => ({{ available: true, update_status: {{ latest_update_action: null }} }}) }};
    if (url.includes('/update-plan')) return {{ ok: true, status: 200, json: async () => ({{ available: false }}) }};
    return {{
      ok: true,
      status: 200,
      json: async () => ({{
        project: {{
          id: 'f55f34521cf1e43173e51795',
          display_name: 'local-ai-packaged',
          source: 'compose',
          confidence: 'high',
          freshness: {{ state: 'fresh' }}
        }}
      }})
    }};
  }};

  const ctrl = m.createProjectController({{
    scheduler: {{ register: () => {{}} }},
    stateClass: s => s,
    escapeHtml: s => String(s)
  }});

  await ctrl.selectProject('f55f34521cf1e43173e51795');
  console.log(JSON.stringify({{
    html: elements['projectDetail'].innerHTML
  }}));
}}).catch(e => {{
  console.error(e);
  process.exit(1);
}});
"""

    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[1],
        check=True,
    )
    output = json.loads(result.stdout)
    html = output["html"]

    # Summary counts
    assert "25 declared" in html
    assert "21 running" in html
    assert "12 current" in html
    assert "4 update available" in html
    assert "7 not applicable" in html
    assert "2 unknown" in html
    assert "0 drift" in html

    # Visual treatment badges
    assert "badge status-current" in html
    assert "badge status-update_available" in html
    assert "badge status-not_applicable" in html
    assert "badge status-unknown" in html

    # Distinct digests side by side
    assert "sha256:111111" in html
    assert "sha256:222222" in html

    # Special candidate reasons
    assert "local_build" in html
    assert "disabled_by_profile" in html
    assert "network_budget_exhausted" in html
    assert "up_to_date" in html
    assert "candidate_digest_differs" in html

    # Local build shows not_applicable, no invented candidate
    assert "(build)" in html

    # Dependencies displayed without execution semantics
    assert "dep-pill" in html
    assert "auth" in html

    # Provenance verified indicator
    assert "provenance-verified" in html
    assert "provenance-unverified" in html


def test_10_node_runtime_error_states():
    """Verify bounded user-readable error states without raw error dump."""
    script = """
global.window = global;
import('./src/aipm/dashboard/static/mission-control-projects.js').then(async m => {
  const elements = {};
  global.document = {
    getElementById: (id) => {
      if (!elements[id]) elements[id] = { textContent: '', className: '', innerHTML: '', style: {} };
      return elements[id];
    },
    querySelectorAll: () => []
  };

  const results = {};

  const runTest = async (testName, fetchImpl) => {
    global.fetch = fetchImpl;
    const ctrl = m.createProjectController({
      scheduler: { register: () => {} },
      stateClass: s => s,
      escapeHtml: s => String(s)
    });
    await ctrl.selectProject('test-proj');
    results[testName] = elements['projectDetail'].innerHTML;
  };

  const defaultNonCompose = (url) => {
    if (url.endsWith('/health')) return { ok: true, status: 200, json: async () => ({}) };
    if (url.endsWith('/containers')) return { ok: true, status: 200, json: async () => ({ containers: [] }) };
    if (url.endsWith('/update/status')) return { ok: true, status: 200, json: async () => ({ available: false }) };
    if (url.includes('/update-plan')) return { ok: true, status: 200, json: async () => ({ available: false }) };
    return { ok: true, status: 200, json: async () => ({ project: { id: 'test-proj', display_name: 'test' } }) };
  };

  // 1. Auth required (401)
  await runTest('auth', async (url) => {
    if (url.includes('/compose-intelligence')) return { ok: false, status: 401, json: async () => ({}) };
    return defaultNonCompose(url);
  });

  // 2. Not found (404)
  await runTest('not_found', async (url) => {
    if (url.includes('/compose-intelligence')) return { ok: false, status: 404, json: async () => ({}) };
    return defaultNonCompose(url);
  });

  // 3. Unavailable (500 / COMPOSE_UNAVAILABLE)
  await runTest('unavailable', async (url) => {
    if (url.includes('/compose-intelligence')) return { ok: false, status: 500, json: async () => ({}) };
    return defaultNonCompose(url);
  });

  // 4. Observation failed
  await runTest('observation_failed', async (url) => {
    if (url.includes('/compose-intelligence')) {
      return { ok: true, status: 200, json: async () => ({ available: false, error: 'COMPOSE_OBSERVATION_FAILED', project: null }) };
    }
    return defaultNonCompose(url);
  });

  // 5. Network timeout
  await runTest('timeout', async (url) => {
    if (url.includes('/compose-intelligence')) throw new Error('Timeout');
    return defaultNonCompose(url);
  });

  console.log(JSON.stringify(results));
}).catch(e => {
  console.error(e);
  process.exit(1);
});
"""

    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[1],
        check=True,
    )
    results = json.loads(result.stdout)

    assert "Authentication required to observe service intelligence." in results["auth"]
    assert "No Compose configuration found for this project." in results["not_found"]
    assert (
        "Compose service intelligence is not available for this project."
        in results["unavailable"]
    )
    assert "Compose service observation failed." in results["observation_failed"]
    assert (
        "Service intelligence request timed out or network is unavailable."
        in results["timeout"]
    )

    # None should dump raw JSON stack or errors in the compose section
    for key, html in results.items():
        assert (
            "{"
            not in html.split('id="projectComposeIntelligence-test-proj"')[1]
            .split('empty">')[1]
            .split("</div>")[0]
        )
        assert "Traceback" not in html
        assert (
            "undefined"
            not in html.split('id="projectComposeIntelligence-test-proj"')[1]
        )


def test_11_node_runtime_refresh_interaction():
    """Verify refresh button re-invokes GET /compose-intelligence and updates DOM without mutations."""
    script = """
global.window = global;
import('./src/aipm/dashboard/static/mission-control-projects.js').then(async m => {
  let composeCalls = 0;
  let clickedListener = null;

  const elements = {
    'projectComposeIntelligence-p1': {
      set outerHTML(val) { this._html = val; },
      get outerHTML() { return this._html; }
    }
  };

  global.document = {
    getElementById: (id) => {
      if (!elements[id]) elements[id] = { textContent: '', className: '', innerHTML: '', style: {} };
      return elements[id];
    },
    querySelectorAll: (sel) => {
      if (sel.includes('data-refresh-compose')) {
        return [{
          disabled: false,
          textContent: 'Refresh',
          dataset: { refreshCompose: 'p1' },
          addEventListener: (evt, handler) => { clickedListener = handler; }
        }];
      }
      return [];
    }
  };

  global.fetch = async (url) => {
    if (url.includes('/compose-intelligence')) {
      composeCalls++;
      return {
        ok: true,
        status: 200,
        json: async () => ({
          available: true,
          status: 'ok',
          project: {
            id: 'p1',
            compose_identity: 'p1-compose',
            total_services_count: 5,
            running_services_count: 5,
            current_count: 5,
            updates_available_count: 0,
            not_applicable_count: 0,
            unknown_count: 0,
            drift_count: 0,
            freshness: 'fresh',
            services: []
          }
        })
      };
    }
    if (url.endsWith('/health')) return { ok: true, status: 200, json: async () => ({}) };
    if (url.endsWith('/containers')) return { ok: true, status: 200, json: async () => ({ containers: [] }) };
    if (url.endsWith('/update/status')) return { ok: true, status: 200, json: async () => ({ available: false }) };
    if (url.includes('/update-plan')) return { ok: true, status: 200, json: async () => ({ available: false }) };
    return { ok: true, status: 200, json: async () => ({ project: { id: 'p1', display_name: 'test' } }) };
  };

  const ctrl = m.createProjectController({
    scheduler: { register: () => {} },
    stateClass: s => s,
    escapeHtml: s => String(s)
  });

  await ctrl.selectProject('p1');
  const initialCalls = composeCalls;
  if (clickedListener) {
    await clickedListener();
  }
  const afterCalls = composeCalls;

  console.log(JSON.stringify({ initialCalls, afterCalls }));
}).catch(e => {
  console.error(e);
  process.exit(1);
});
"""

    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[1],
        check=True,
    )
    data = json.loads(result.stdout)
    assert data["initialCalls"] == 1
    assert data["afterCalls"] == 2
