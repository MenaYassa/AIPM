export const OBSERVATION_STATES = Object.freeze([
  'fresh',
  'stale',
  'unavailable',
  'never_sampled',
  'unknown',
  'error',
]);

const UI_STATES = new Set([
  'healthy',
  'fresh',
  'warning',
  'stale',
  'critical',
  'unavailable',
  'never_sampled',
  'unknown',
  'error',
]);

export function normalizeObservationState(value, fallback = 'unknown') {
  return OBSERVATION_STATES.includes(value) ? value : fallback;
}

export function normalizeEnvelope(payload) {
  const data = payload && typeof payload === 'object' ? payload : {};
  const available = data.available === true;
  const transportOk = data.transport_ok !== false;
  const state = normalizeObservationState(
    data.state || (available ? 'fresh' : transportOk ? 'unavailable' : 'error'),
  );
  return {
    available,
    transportOk,
    state,
    data,
    error: typeof data.error === 'string' ? data.error : null,
  };
}

export function stateClass(value) {
  return UI_STATES.has(value) ? value : 'unknown';
}
