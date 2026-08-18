export function createLogsController({ scheduler, stateClass, escapeHtml }) {
  let source = 'aipm-dashboard';
  let severity = '';
  let range = '24h';
  let limit = 100;

  const $ = (id) => document.getElementById(id);
  const esc = escapeHtml;

  function render(data) {
    const observation = data.observation || { state: 'unknown', available: false };
    const state = observation.state || 'unknown';
    const sourceInfo = data.source || {};
    $('logsObservationState').textContent = state.replaceAll('_', ' ');
    $('logsObservationState').className = `badge ${stateClass(state)}`;
    $('logsObservationAge').textContent = observation.age_seconds == null
      ? (state === 'never_sampled' ? 'Never sampled' : 'Sample age unavailable')
      : `Sample age ${observation.age_seconds}s`;
    $('logsSourceLabel').textContent = sourceInfo.label || 'Backend-owned source';
    $('logsCounts').textContent = `${data.returned_lines || 0} lines · ${data.returned_bytes || 0} bytes${data.truncated ? ' · truncated' : ''}`;
    const sources = data.sources || [];
    const selector = $('logsSource');
    if (selector.options.length !== sources.length) {
      selector.innerHTML = sources.map(item => `<option value="${esc(item.id)}">${esc(item.label)} · ${esc(item.owner)}</option>`).join('');
    }
    selector.value = source;
    const entries = data.entries || [];
    $('logsEntries').innerHTML = entries.length ? entries.map(entry => `<article class="log-entry"><div class="log-entry-meta"><span class="badge ${stateClass(entry.severity)}">${esc(entry.severity)}</span><time>${esc(entry.timestamp || '—')}</time>${entry.unit ? `<span>${esc(entry.unit)}</span>` : ''}${entry.project ? `<span>${esc(entry.project)}</span>` : ''}${entry.redacted ? '<span class="pill warning">redacted</span>' : ''}</div><pre>${esc(entry.message)}</pre>${entry.evidence?.length ? `<div class="subtle">Redaction evidence: ${esc(entry.evidence.join(', '))}</div>` : ''}</article>`).join('') : `<div class="empty">${esc(data.errors?.[0]?.message || (observation.available ? 'No log entries in this bounded window.' : 'Logs unavailable from this source.'))}</div>`;
    const error = data.errors?.[0];
    $('logsError').textContent = error ? `${error.code}: ${error.message}` : '';
    $('logsTruncation').textContent = data.truncated ? 'Result bounded; use the opaque next cursor for the next page.' : 'Result is within the requested bounds.';
  }

  function query() {
    const durations = { '1h': 3600000, '6h': 21600000, '24h': 86400000 };
    const since = new Date(Date.now() - (durations[range] || durations['24h'])).toISOString();
    const params = new URLSearchParams({ source, since, limit: String(limit) });
    if (severity) params.set('severity', severity);
    return fetch(`/api/logs?${params.toString()}`, { cache: 'no-store' }).then(async response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    });
  }

  $('logsSource').addEventListener('change', (event) => { source = event.target.value; void scheduler.refresh('logs'); });
  $('logsSeverity').addEventListener('change', (event) => { severity = event.target.value; void scheduler.refresh('logs'); });
  $('logsRange').addEventListener('change', (event) => { range = event.target.value; void scheduler.refresh('logs'); });
  $('logsLimit').addEventListener('change', (event) => { limit = Number(event.target.value) || 100; void scheduler.refresh('logs'); });

  return { load: query };
}
