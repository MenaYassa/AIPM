from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
MODULE = ROOT / "src/aipm/dashboard/static/mission-control-advisor-fixture.js"
INDEX = ROOT / "src/aipm/dashboard/static/index.html"


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


def test_ai_agent_route_is_fixture_only_and_resource_history_is_unchanged() -> None:
    source = INDEX.read_text()
    advisor_start = source.index('<div class="view advisor-fixture-view" data-view="ai-agent" id="advisorFixtureRoot" hidden>')
    advisor_end = source.index('</div>\n      </main>', advisor_start) + len('</div>')
    ai_agent = source[advisor_start:advisor_end]
    assert 'id="advisorFixtureRoot"' in ai_agent
    assert 'data-advisor-fixture="normal"' in ai_agent
    assert 'data-advisor-fixture="http-500"' in ai_agent
    assert "fixture presentation" in ai_agent.lower()
    assert "/api/advisor/evaluate" not in ai_agent
    assert "export_telemetry_snapshot" not in ai_agent
    assert "compose_advisor" not in ai_agent
    assert "scheduler.register('history',loadHistory,{intervalMs:60000})" in source
    assert "fetch(`/api/history/host?range=${encodeURIComponent(historyRange)}&limit=240`" in source
    assert "No historical samples" in source
