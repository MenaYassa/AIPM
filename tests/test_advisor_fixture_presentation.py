from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
MODULE = ROOT / "src/aipm/dashboard/static/mission-control-advisor-fixture.js"
INDEX = ROOT / "src/aipm/dashboard/static/index.html"
PROVIDER = ROOT / "src/aipm/dashboard/static/mission-control-advisor-provider.js"


def _node(expression: str) -> dict:
    script = f"""
import * as fixture from {json.dumps(MODULE.as_uri())};
{expression}
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_all_fixture_states_are_bounded_and_renderable() -> None:
    result = _node(
        """
const keys = [...fixture.ADVISOR_FIXTURE_KEYS];
const rendered = keys.map((key) => {
  const value = fixture.getAdvisorFixture(key);
  const presentation = fixture.buildAdvisorPresentation(value);
  return {
    key,
    frozen: Object.isFrozen(value) && Object.isFrozen(presentation),
    kind: presentation.kind,
    status: presentation.status ?? presentation.code,
    code: presentation.code ?? null,
    findingCount: presentation.findings?.length ?? 0,
    recommendationCount: presentation.recommendations?.length ?? 0,
    uncertaintyCount: presentation.uncertainties?.length ?? 0,
    htmlSafe: JSON.stringify(presentation).includes('<') === false,
  };
});
console.log(JSON.stringify({keys, rendered}));
"""
    )
    assert result["keys"] == [
        "normal",
        "empty",
        "unavailable",
        "stale",
        "invalid",
        "incomplete",
        "http-400",
        "http-401",
        "http-422",
        "http-500",
    ]
    assert all(item["frozen"] for item in result["rendered"])
    assert all(item["htmlSafe"] for item in result["rendered"])
    statuses = {item["key"]: item["status"] for item in result["rendered"]}
    assert statuses["normal"] == "fresh"
    assert statuses["unavailable"] == "unavailable"
    assert statuses["stale"] == "stale"
    assert statuses["invalid"] == "partial"
    assert statuses["incomplete"] == "partial"
    transport = {item["key"]: item for item in result["rendered"] if item["key"].startswith("http-")}
    assert {key: item["status"] for key, item in transport.items()} == {
        "http-400": 400,
        "http-401": 401,
        "http-422": 422,
        "http-500": 500,
    }
    assert {key: item["code"] for key, item in transport.items()} == {
        "http-400": "MALFORMED_REQUEST",
        "http-401": "AUTHENTICATION_REQUIRED",
        "http-422": "VALIDATION_ERROR",
        "http-500": "INTERNAL_ERROR",
    }


def test_presentation_preserves_response_values_without_semantic_rewriting() -> None:
    result = _node(
        """
const response = fixture.getAdvisorFixture('normal');
const presentation = fixture.buildAdvisorPresentation(response);
console.log(JSON.stringify({
  request_id: presentation.request_id,
  evaluation_time: presentation.evaluation_time,
  generated_at: presentation.generated_at,
  scope: presentation.scope,
  finding: presentation.findings[0],
  recommendation: presentation.recommendations[0],
}));
"""
    )
    assert result["request_id"] == "fixture-normal-001"
    assert result["evaluation_time"] == "2026-08-24T12:00:00+00:00"
    assert result["generated_at"] == result["evaluation_time"]
    assert result["scope"] == "host"
    assert result["finding"]["category"] == "resource_pressure"
    assert result["finding"]["evidence_refs"] == ["telemetry-history-memory-001"]
    assert result["recommendation"]["finding_refs"] == ["finding-resource-pressure-001"]
    assert result["recommendation"]["evidence_refs"] == ["telemetry-history-memory-001"]


def test_resource_history_summary_is_bounded_and_separate_from_findings() -> None:
    result = _node(
        """
const elements = new Map([
  ['[data-advisor-fixture-body]', { innerHTML: '' }],
  ['[data-advisor-fixture-label]', { textContent: '' }],
]);
const response = {
  ...fixture.getAdvisorFixture('empty'),
  resource_history_summary: [
    { metric: 'disk_percent', state: 'complete', valid_point_count: 6, temporal_span_seconds: 300, cadence_seconds: 60, peak_value: 56.1, peak_observed_at: '2026-08-26T13:35:10+00:00' },
    { metric: 'cpu_percent', state: 'complete', valid_point_count: 6, temporal_span_seconds: 300, cadence_seconds: 60, peak_value: 35.1, peak_observed_at: '2026-08-26T13:35:10+00:00' },
    { metric: 'memory_percent', state: 'complete', valid_point_count: 6, temporal_span_seconds: 300, cadence_seconds: 60, peak_value: 56.4, peak_observed_at: '2026-08-26T13:35:10+00:00' },
  ],
};
const presentation = fixture.renderAdvisorResponse({ querySelector: (selector) => elements.get(selector) }, response, 'live');
console.log(JSON.stringify({
  metrics: presentation.resource_history_summary.map((item) => item.metric),
  html: elements.get('[data-advisor-fixture-body]').innerHTML,
  findings: presentation.findings.length,
  recommendations: presentation.recommendations.length,
}));
"""
    )
    assert result["metrics"] == ["disk_percent", "cpu_percent", "memory_percent"]
    assert 'data-resource-history-metric="cpu_percent"' in result["html"]
    assert "Complete evidence" in result["html"]
    assert "35.1" in result["html"]
    assert "Evidence sufficiency is separate from findings and recommendations." in result["html"]
    assert result["findings"] == 0
    assert result["recommendations"] == 0


def test_degraded_resource_history_summary_states_remain_explicit() -> None:
    result = _node(
        """
const elements = new Map([
  ['[data-advisor-fixture-body]', { innerHTML: '' }],
  ['[data-advisor-fixture-label]', { textContent: '' }],
]);
const response = {
  ...fixture.getAdvisorFixture('empty'),
  resource_history_summary: [
    { metric: 'cpu_percent', state: 'incomplete', valid_point_count: 5, temporal_span_seconds: 240, cadence_seconds: 60, peak_value: 35, peak_observed_at: '2026-08-26T13:34:10+00:00' },
    { metric: 'memory_percent', state: 'unavailable', valid_point_count: 0, temporal_span_seconds: 0, cadence_seconds: 60, peak_value: null, peak_observed_at: null },
    { metric: 'disk_percent', state: 'invalid', valid_point_count: 0, temporal_span_seconds: 0, cadence_seconds: 60, peak_value: null, peak_observed_at: null },
  ],
};
fixture.renderAdvisorResponse({ querySelector: (selector) => elements.get(selector) }, response, 'live');
console.log(JSON.stringify({ html: elements.get('[data-advisor-fixture-body]').innerHTML }));
"""
    )
    assert "Incomplete evidence" in result["html"]
    assert "Evidence unavailable" in result["html"]
    assert "Invalid evidence" in result["html"]
    assert "maximum_gap" not in result["html"]


def test_empty_and_degraded_states_remain_visible() -> None:
    result = _node(
        """
const keys = ['empty', 'unavailable', 'stale', 'invalid', 'incomplete'];
const output = {};
for (const key of keys) {
  const value = fixture.buildAdvisorPresentation(fixture.getAdvisorFixture(key));
  output[key] = {
    status: value.status,
    available: value.available,
    findingCount: value.findings.length,
    recommendationCount: value.recommendations.length,
    uncertaintyKinds: value.uncertainties.map((item) => item.kind),
    coverage: value.evidence_coverage,
  };
}
console.log(JSON.stringify(output));
"""
    )
    assert result["empty"]["findingCount"] == 0
    assert result["empty"]["recommendationCount"] == 0
    assert result["unavailable"]["available"] is False
    assert result["unavailable"]["uncertaintyKinds"] == ["unavailable_source"]
    assert result["stale"]["uncertaintyKinds"] == ["stale_evidence"]
    assert result["invalid"]["uncertaintyKinds"] == ["invalid_evidence"]
    assert result["incomplete"]["uncertaintyKinds"] == ["missing_evidence"]
    assert result["incomplete"]["coverage"][0]["omitted"] == 9


def test_transport_error_states_preserve_safe_codes_and_fields() -> None:
    result = _node(
        """
const output = {};
for (const key of ['http-400', 'http-401', 'http-422', 'http-500']) {
  const value = fixture.buildAdvisorPresentation(fixture.getAdvisorFixture(key));
  output[key] = {kind: value.kind, status: value.status, code: value.code, fields: value.fields};
}
console.log(JSON.stringify(output));
"""
    )
    assert result["http-400"] == {
        "kind": "transport_error",
        "status": 400,
        "code": "MALFORMED_REQUEST",
        "fields": [],
    }
    assert result["http-401"]["code"] == "AUTHENTICATION_REQUIRED"
    assert result["http-422"]["fields"] == [{"path": "body", "code": "invalid", "message": "request failed validation"}]
    assert result["http-500"]["code"] == "INTERNAL_ERROR"


def test_fixture_module_has_no_live_access_or_browser_clock() -> None:
    source = MODULE.read_text()
    lowered = source.lower()
    for token in ("fetch(", "xmlhttprequest", "websocket", "sqlite", "filesystem", "process.", "systemctl", "docker", "git", "cloudflared", "datetime.now", "date.now", "new date(", "math.random", "crypto."):
        assert token not in lowered


def test_live_provider_uses_only_bounded_get_and_keeps_fixture_mode_explicit() -> None:
    source = PROVIDER.read_text()
    lowered = source.lower()
    assert "const advisor_live_route = '/api/advisor';" in lowered
    assert "fetchimpl(advisor_live_route, { cache: 'no-store' })" in lowered
    assert "post /api/advisor/evaluate" not in lowered
    for token in ("new date(", "date.now", "math.random", "setinterval", "settimeout", "sqlite", "filesystem", "systemctl", "docker", "git"):
        assert token not in lowered
    assert "renderfixture" in lowered
    assert "renderlive" in lowered


def test_ai_agent_route_has_explicit_live_and_fixture_modes_and_resource_history_is_unchanged() -> None:
    source = INDEX.read_text()
    advisor_start = source.index('<div class="view advisor-fixture-view" data-view="ai-agent" id="advisorFixtureRoot" hidden>')
    advisor_end = source.index('</div>\n      </main>', advisor_start) + len('</div>')
    ai_agent = source[advisor_start:advisor_end]
    assert 'id="advisorFixtureRoot"' in ai_agent
    assert 'data-advisor-mode="live"' in ai_agent
    assert 'data-advisor-mode="fixture"' in ai_agent
    assert 'data-advisor-fixture="normal"' in ai_agent
    assert 'data-advisor-fixture="http-500"' in ai_agent
    assert "fixture mode" in ai_agent.lower()
    assert "mission-control-advisor-provider.js" in source
    assert "/api/advisor/evaluate" not in ai_agent
    assert "export_telemetry_snapshot" not in ai_agent
    assert "compose_advisor" not in ai_agent
    assert "scheduler.register('history',loadHistory,{intervalMs:60000})" in source
    assert "fetch(`/api/history/host?range=${encodeURIComponent(historyRange)}&limit=240`" in source
    assert "No historical samples" in source


def _node_provider(expression: str) -> dict:
    script = f"""
import * as provider from {json.dumps(PROVIDER.as_uri())};
import * as fixture from {json.dumps(MODULE.as_uri())};
{expression}
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_live_provider_fetches_once_and_fixture_mode_does_not_mix_data() -> None:
    result = _node_provider(
        """
const elements = new Map([
  ['[data-advisor-fixture-controls]', {hidden: false}],
  ['[data-advisor-live-controls]', {hidden: false}],
  ['[data-advisor-mode-label]', {textContent: ''}],
  ['[data-advisor-fixture-label]', {textContent: ''}],
  ['[data-advisor-fixture-body]', {innerHTML: ''}],
]);
const root = {
  dataset: {},
  querySelector(selector) { return elements.get(selector); },
};
let calls = 0;
let request;
const instance = provider.createAdvisorProvider(root, {
  fetchImpl: async (url, options) => {
    calls += 1;
    request = {url, options};
    return {ok: true, status: 200, json: async () => fixture.getAdvisorFixture('empty')};
  },
});
const live = await instance.renderLive();
const liveBody = elements.get('[data-advisor-fixture-body]').innerHTML;
const selectedFixture = instance.renderFixture('normal');
console.log(JSON.stringify({
  mode: instance.mode,
  calls,
  url: request.url,
  cache: request.options.cache,
  liveStatus: live.status,
  fixtureStatus: selectedFixture.status,
  fixtureBody: elements.get('[data-advisor-fixture-body]').innerHTML,
  liveBodyHadNormalFinding: liveBody.includes('Host memory pressure observed'),
  fixtureBodyHasNormalFinding: elements.get('[data-advisor-fixture-body]').innerHTML.includes('Host memory pressure observed'),
}));
"""
    )
    assert result["mode"] == "fixture"
    assert result["calls"] == 1
    assert result["url"] == "/api/advisor"
    assert result["cache"] == "no-store"
    assert result["liveStatus"] == "fresh"
    assert result["fixtureStatus"] == "fresh"
    assert result["liveBodyHadNormalFinding"] is False
    assert result["fixtureBodyHasNormalFinding"] is True


def test_live_provider_maps_transport_failure_to_safe_bounded_error() -> None:
    result = _node_provider(
        """
const elements = new Map([
  ['[data-advisor-fixture-controls]', {hidden: false}],
  ['[data-advisor-live-controls]', {hidden: false}],
  ['[data-advisor-mode-label]', {textContent: ''}],
  ['[data-advisor-fixture-label]', {textContent: ''}],
  ['[data-advisor-fixture-body]', {innerHTML: ''}],
]);
const root = {dataset: {}, querySelector(selector) { return elements.get(selector); }};
const instance = provider.createAdvisorProvider(root, {
  fetchImpl: async () => ({
    ok: false,
    status: 503,
    json: async () => ({error: {code: 'ADVISOR_UNAVAILABLE', message: 'Advisor evaluation is unavailable', fields: []}}),
  }),
});
const output = await instance.renderLive();
console.log(JSON.stringify({kind: output.kind, status: output.status, code: output.code, mode: instance.mode}));
"""
    )
    assert result == {"kind": "transport_error", "status": 503, "code": "ADVISOR_UNAVAILABLE", "mode": "live"}
