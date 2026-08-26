const MAX_FINDINGS = 50;
const MAX_RECOMMENDATIONS = 25;
const MAX_UNCERTAINTIES = 32;
const MAX_COVERAGE = 32;
const MAX_HISTORY_SUMMARY = 3;
const MAX_FIELDS = 10;

function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

const normalResponse = {
  schema_version: 'advisor.v1',
  request_id: 'fixture-normal-001',
  available: true,
  status: 'fresh',
  evaluation_time: '2026-08-24T12:00:00+00:00',
  generated_at: '2026-08-24T12:00:00+00:00',
  freshness_deadline: '2026-08-24T12:05:00+00:00',
  scope: 'host',
  findings: [{
    finding_id: 'finding-resource-pressure-001',
    category: 'resource_pressure',
    severity: 'warning',
    confidence: 'high',
    title: 'Host memory pressure observed',
    condition: 'memory_percent reached 86 percent in the observed window',
    rule_id: 'resource-pressure-memory',
    rule_version: '1',
    evidence_refs: ['telemetry-history-memory-001'],
    uncertainty_refs: [],
    provenance_refs: [],
    safe_links: [],
  }],
  recommendations: [{
    recommendation_id: 'recommendation-resource-pressure-001',
    category: 'resource_pressure',
    priority: 2,
    status: 'new',
    title: 'Review the recent memory-pressure evidence',
    summary: 'Compare the recent memory observation with the surrounding host history.',
    rationale: 'The deterministic resource-pressure rule identified a bounded high-memory observation.',
    confidence: 'high',
    finding_refs: ['finding-resource-pressure-001'],
    evidence_refs: ['telemetry-history-memory-001'],
    uncertainty_refs: [],
    safe_links: [],
    provenance_refs: [],
  }],
  uncertainties: [],
  provenance: [],
  evidence_coverage: [{ source_id: 'history', expected: 15, observed: 15, stale: 0, unavailable: 0, invalid: 0, omitted: 0 }],
  links: [],
  next_cursor: null,
};

const emptyResponse = {
  ...normalResponse,
  request_id: 'fixture-empty-001',
  findings: [],
  recommendations: [],
};

const unavailableResponse = {
  ...normalResponse,
  request_id: 'fixture-unavailable-001',
  available: false,
  status: 'unavailable',
  findings: [],
  recommendations: [],
  uncertainties: [{
    uncertainty_id: 'uncertainty-source-unavailable-001',
    kind: 'unavailable_source',
    summary: 'The history source was unavailable for this evaluation.',
    evidence_refs: [],
    confidence_impact: 'to_unknown',
    resolution_hint: 'Review the source availability before interpreting this response.',
  }],
  evidence_coverage: [{ source_id: 'history', expected: 15, observed: 0, stale: 0, unavailable: 15, invalid: 0, omitted: 0 }],
};

const staleResponse = {
  ...normalResponse,
  request_id: 'fixture-stale-001',
  status: 'stale',
  findings: [],
  recommendations: [],
  uncertainties: [{
    uncertainty_id: 'uncertainty-stale-001',
    kind: 'stale_evidence',
    summary: 'The available history is older than its freshness deadline.',
    evidence_refs: ['telemetry-history-memory-001'],
    confidence_impact: 'to_unknown',
    resolution_hint: 'Review the evidence freshness before relying on this view.',
  }],
  evidence_coverage: [{ source_id: 'history', expected: 15, observed: 15, stale: 15, unavailable: 0, invalid: 0, omitted: 0 }],
};

const invalidResponse = {
  ...normalResponse,
  request_id: 'fixture-invalid-001',
  status: 'partial',
  findings: [],
  recommendations: [],
  uncertainties: [{
    uncertainty_id: 'uncertainty-invalid-001',
    kind: 'invalid_evidence',
    summary: 'Some source records failed bounded validation and were withheld.',
    evidence_refs: [],
    confidence_impact: 'withheld',
    resolution_hint: 'Review the source validation result before interpreting omitted values.',
  }],
  evidence_coverage: [{ source_id: 'history', expected: 15, observed: 12, stale: 0, unavailable: 0, invalid: 3, omitted: 0 }],
};

const incompleteResponse = {
  ...normalResponse,
  request_id: 'fixture-incomplete-001',
  status: 'partial',
  findings: [],
  recommendations: [],
  uncertainties: [{
    uncertainty_id: 'uncertainty-incomplete-001',
    kind: 'missing_evidence',
    summary: 'The persisted history does not cover the complete requested window.',
    evidence_refs: [],
    confidence_impact: 'to_unknown',
    resolution_hint: 'Review the available history before treating absence as a condition.',
  }],
  evidence_coverage: [{ source_id: 'history', expected: 15, observed: 6, stale: 0, unavailable: 0, invalid: 0, omitted: 9 }],
};

const fixtures = {
  normal: normalResponse,
  empty: emptyResponse,
  unavailable: unavailableResponse,
  stale: staleResponse,
  invalid: invalidResponse,
  incomplete: incompleteResponse,
};

const transportErrors = {
  'http-400': { kind: 'transport_error', status: 400, code: 'MALFORMED_REQUEST', message: 'Malformed advisor request body', fields: [] },
  'http-401': { kind: 'transport_error', status: 401, code: 'AUTHENTICATION_REQUIRED', message: 'Advisor authentication is required', fields: [] },
  'http-422': { kind: 'transport_error', status: 422, code: 'VALIDATION_ERROR', message: 'Advisor request failed validation', fields: [{ path: 'body', code: 'invalid', message: 'request failed validation' }] },
  'http-500': { kind: 'transport_error', status: 500, code: 'INTERNAL_ERROR', message: 'Advisor evaluation failed', fields: [] },
};

export const ADVISOR_FIXTURE_KEYS = Object.freeze([
  'normal',
  'empty',
  'unavailable',
  'stale',
  'invalid',
  'incomplete',
  'http-400',
  'http-401',
  'http-422',
  'http-500',
]);

export const ADVISOR_FIXTURES = freeze({ ...fixtures, ...transportErrors });

export function getAdvisorFixture(key = 'normal') {
  if (!Object.prototype.hasOwnProperty.call(ADVISOR_FIXTURES, key)) {
    throw new Error('Unknown advisor fixture');
  }
  return ADVISOR_FIXTURES[key];
}

export function buildAdvisorPresentation(response) {
  const isTransportError = response?.kind === 'transport_error';
  if (isTransportError) {
    return freeze({
      kind: response.kind,
      status: response.status,
      code: response.code,
      message: response.message,
      fields: response.fields.slice(0, MAX_FIELDS),
    });
  }
  return freeze({
    kind: 'advisor_response',
    request_id: response.request_id,
    available: response.available,
    status: response.status,
    evaluation_time: response.evaluation_time,
    generated_at: response.generated_at,
    scope: response.scope,
    findings: response.findings.slice(0, MAX_FINDINGS),
    recommendations: response.recommendations.slice(0, MAX_RECOMMENDATIONS),
    uncertainties: response.uncertainties.slice(0, MAX_UNCERTAINTIES),
    evidence_coverage: response.evidence_coverage.slice(0, MAX_COVERAGE),
    resource_history_summary: Array.isArray(response.resource_history_summary) ? response.resource_history_summary.slice(0, MAX_HISTORY_SUMMARY) : [],
  });
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[character]));
}

function statusClass(status) {
  return ['fresh', 'partial', 'stale', 'unavailable', 'error'].includes(status) ? status : 'unknown';
}

function historySummaryStateLabel(state) {
  return {
    complete: 'Complete evidence',
    incomplete: 'Incomplete evidence',
    unavailable: 'Evidence unavailable',
    stale: 'Stale evidence',
    invalid: 'Invalid evidence',
  }[state] || 'Evidence state unknown';
}

function responseMarkup(view) {
  if (view.kind === 'transport_error') {
    const fields = view.fields.length
      ? `<div class="advisor-error-fields">${view.fields.map((field) => `<span>${escapeHtml(field.path)} · ${escapeHtml(field.code)}</span>`).join('')}</div>`
      : '';
    return `<div class="advisor-state advisor-error" role="alert"><span class="badge unavailable">HTTP ${escapeHtml(view.status)}</span><h3>${escapeHtml(view.code)}</h3><p>${escapeHtml(view.message)}</p>${fields}</div>`;
  }

  const findingMarkup = view.findings.length
    ? view.findings.map((finding) => `<article class="advisor-item"><div class="advisor-item-head"><strong>${escapeHtml(finding.title)}</strong><span class="badge ${escapeHtml(finding.severity)}">${escapeHtml(finding.severity)}</span></div><p>${escapeHtml(finding.condition)}</p><small>${escapeHtml(finding.category)} · ${escapeHtml(finding.confidence)} · ${escapeHtml(finding.rule_id)} v${escapeHtml(finding.rule_version)}</small></article>`).join('')
    : '<div class="empty">No findings in this fixture.</div>';
  const recommendationMarkup = view.recommendations.length
    ? view.recommendations.map((recommendation) => `<article class="advisor-item"><div class="advisor-item-head"><strong>${escapeHtml(recommendation.title)}</strong><span class="badge ${escapeHtml(recommendation.status)}">${escapeHtml(recommendation.status)}</span></div><p>${escapeHtml(recommendation.summary)}</p><small>Priority ${escapeHtml(recommendation.priority)} · ${escapeHtml(recommendation.category)} · ${escapeHtml(recommendation.confidence)}</small></article>`).join('')
    : '<div class="empty">No recommendations in this fixture.</div>';
  const uncertaintyMarkup = view.uncertainties.length
    ? view.uncertainties.map((uncertainty) => `<article class="advisor-item"><div class="advisor-item-head"><strong>${escapeHtml(uncertainty.kind)}</strong><span class="badge warning">uncertainty</span></div><p>${escapeHtml(uncertainty.summary)}</p><small>${escapeHtml(uncertainty.resolution_hint || 'No resolution hint supplied.')}</small></article>`).join('')
    : '<div class="empty">No uncertainty records in this fixture.</div>';
  const coverageMarkup = view.evidence_coverage.length
    ? view.evidence_coverage.map((coverage) => `<div class="advisor-coverage-row"><span>${escapeHtml(coverage.source_id)}</span><strong>${escapeHtml(coverage.observed)} / ${escapeHtml(coverage.expected)}</strong><small>stale ${escapeHtml(coverage.stale)} · unavailable ${escapeHtml(coverage.unavailable)} · invalid ${escapeHtml(coverage.invalid)} · omitted ${escapeHtml(coverage.omitted)}</small></div>`).join('')
    : '<div class="empty">No coverage records in this fixture.</div>';
  const historySummaryMarkup = view.resource_history_summary.length
    ? view.resource_history_summary.map((summary) => {
      const peak = summary.peak_value == null
        ? 'peak unavailable'
        : `peak ${escapeHtml(summary.peak_value)}% at ${escapeHtml(summary.peak_observed_at)}`;
      return `<article class="advisor-item" data-resource-history-metric="${escapeHtml(summary.metric)}"><div class="advisor-item-head"><strong>${escapeHtml(summary.metric)}</strong><span class="badge ${statusClass(summary.state)}">${escapeHtml(historySummaryStateLabel(summary.state))}</span></div><p>${escapeHtml(summary.valid_point_count)} valid points across ${escapeHtml(summary.temporal_span_seconds)} seconds at ${escapeHtml(summary.cadence_seconds)} seconds cadence.</p><small>${peak}</small></article>`;
    }).join('')
    : '<div class="empty">No resource-history summary supplied.</div>';

  return `<div class="advisor-state" aria-live="polite"><div class="advisor-response-meta"><span class="badge ${statusClass(view.status)}">${escapeHtml(view.status)}</span><span>${view.available ? 'Evidence available' : 'Evidence unavailable'}</span><span>Scope: ${escapeHtml(view.scope)}</span></div><p class="subtle">Request ${escapeHtml(view.request_id)} · Evaluation ${escapeHtml(view.evaluation_time)} · Generated ${escapeHtml(view.generated_at)}</p><div class="advisor-grid"><section class="advisor-panel"><div class="section-head"><div><h3>Findings</h3><p>Presented exactly as supplied by the advisor response.</p></div><span class="pill">${view.findings.length}</span></div>${findingMarkup}</section><section class="advisor-panel"><div class="section-head"><div><h3>Recommendations</h3><p>Read-only explanatory output; no action controls.</p></div><span class="pill">${view.recommendations.length}</span></div>${recommendationMarkup}</section><section class="advisor-panel"><div class="section-head"><div><h3>Uncertainty</h3><p>Freshness, availability, invalidity, and coverage limits remain visible.</p></div><span class="pill">${view.uncertainties.length}</span></div>${uncertaintyMarkup}</section><section class="advisor-panel"><div class="section-head"><div><h3>Evidence coverage</h3><p>Bounded source counts from the response.</p></div><span class="pill">${view.evidence_coverage.length}</span></div>${coverageMarkup}</section><section class="advisor-panel"><div class="section-head"><div><h3>Resource history</h3><p>Evidence sufficiency is separate from findings and recommendations.</p></div><span class="pill">${view.resource_history_summary.length}</span></div>${historySummaryMarkup}</section></div></div>`;
}

export function renderAdvisorResponse(root, response, label = 'live') {
  if (!root) throw new Error('Advisor response root is required');
  const presentation = buildAdvisorPresentation(response);
  root.querySelector('[data-advisor-fixture-body]').innerHTML = responseMarkup(presentation);
  root.querySelector('[data-advisor-fixture-label]').textContent = label;
  return presentation;
}

export function renderAdvisorFixture(root, key = 'normal') {
  return renderAdvisorResponse(root, getAdvisorFixture(key), key);
}

export function createAdvisorFixtureController(root) {
  return Object.freeze({
    render(key = 'normal') {
      return renderAdvisorFixture(root, key);
    },
  });
}
