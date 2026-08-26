import {
  ADVISOR_FIXTURE_KEYS,
  createAdvisorFixtureController,
  renderAdvisorResponse,
} from './mission-control-advisor-fixture.js';

export const ADVISOR_LIVE_ROUTE = '/api/advisor';

const SAFE_TRANSPORT_CODES = new Map([
  [400, 'MALFORMED_REQUEST'],
  [401, 'AUTHENTICATION_REQUIRED'],
  [422, 'VALIDATION_ERROR'],
  [500, 'INTERNAL_ERROR'],
  [503, 'ADVISOR_UNAVAILABLE'],
]);

function transportError(status, payload = {}) {
  const error = payload && typeof payload.error === 'object' ? payload.error : {};
  return {
    kind: 'transport_error',
    status: Number.isInteger(status) ? status : 500,
    code: SAFE_TRANSPORT_CODES.get(status) || 'INTERNAL_ERROR',
    message: typeof error.message === 'string' ? error.message : 'Advisor evaluation failed',
    fields: Array.isArray(error.fields) ? error.fields : [],
  };
}

export function createAdvisorProvider(root, { fetchImpl = globalThis.fetch } = {}) {
  if (!root) throw new Error('Advisor provider root is required');
  if (typeof fetchImpl !== 'function') throw new Error('Advisor provider fetch is required');
  const fixtureController = createAdvisorFixtureController(root);
  let mode = 'live';

  function setMode(nextMode) {
    if (nextMode !== 'live' && nextMode !== 'fixture') throw new Error('Unknown advisor provider mode');
    mode = nextMode;
    root.dataset.advisorMode = mode;
    root.querySelector('[data-advisor-fixture-controls]').hidden = mode !== 'fixture';
    root.querySelector('[data-advisor-live-controls]').hidden = mode !== 'live';
    root.querySelector('[data-advisor-mode-label]').textContent = mode === 'live' ? 'live' : 'fixture';
  }

  function renderFixture(key = 'normal') {
    setMode('fixture');
    return fixtureController.render(key);
  }

  async function renderLive() {
    setMode('live');
    const body = root.querySelector('[data-advisor-fixture-body]');
    body.innerHTML = '<div class="empty">Requesting one bounded live advisor assessment…</div>';
    try {
      const response = await fetchImpl(ADVISOR_LIVE_ROUTE, { cache: 'no-store' });
      let payload;
      try {
        payload = await response.json();
      } catch (_error) {
        payload = {};
      }
      if (!response.ok) {
        return renderAdvisorResponse(root, transportError(response.status, payload), 'live');
      }
      return renderAdvisorResponse(root, payload, 'live');
    } catch (_error) {
      return renderAdvisorResponse(root, transportError(503), 'live');
    }
  }

  return Object.freeze({
    get mode() {
      return mode;
    },
    renderFixture,
    renderLive,
    setMode,
    fixtureKeys: ADVISOR_FIXTURE_KEYS,
  });
}
