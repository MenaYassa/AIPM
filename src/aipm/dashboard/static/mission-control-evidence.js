function createEvidenceController({ escapeHtml }) {
  const esc = escapeHtml || (value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char])));
  let incidentCursor = null;
  let timelineIncidentId = null;
  let timelineCursor = null;
  let timelineHasMore = false;
  let timelinePartial = false;
  let timelineEntries = [];
  let timelineError = null;
  let timelineGeneration = 0;
  let timelineInFlight = null;
  let eventDetailSerial = 0;
  let eventDetailRequest = null;

  function renderIncidents(data) {
    const target = document.getElementById('incidentPage');
    if (!target) return;
    const items = data.incidents || [];
    const state = document.getElementById('incidentPageState');
    if (state) state.textContent = data.available ? `${items.length} shown` : 'Unavailable';
    target.innerHTML = data.available && items.length
      ? items.map(item => `<article class="incident-card"><h4>${esc(item.title)}</h4><div class="incident-meta"><span>${esc(item.severity)}</span><span>${esc(item.status)}</span><span>${esc(item.resource?.name || item.resource?.identifier || 'resource')}</span><span>${esc(item.updated_at || item.started_at || '—')}</span></div><p class="subtle">${esc(item.summary)}</p><button type="button" class="evidence-link" data-incident-id="${esc(item.id)}">View persisted timeline</button></article>`).join('')
      : `<div class="empty">${esc(data.error || 'No incidents available.')}</div>`;
    const next = document.getElementById('incidentNext');
    if (next) {
      next.disabled = !data.next_cursor;
      next.onclick = data.next_cursor ? () => { incidentCursor = data.next_cursor; loadIncidentPage(); } : null;
    }
    target.querySelectorAll('[data-incident-id]').forEach(button => {
      button.addEventListener('click', () => loadTimeline(button.dataset.incidentId));
    });
  }

  async function loadIncidentPage() {
    try {
      const cursor = incidentCursor ? `&cursor=${encodeURIComponent(incidentCursor)}` : '';
      const response = await fetch(`/api/incidents?range=7d&status=open&limit=10${cursor}`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderIncidents(await response.json());
    } catch (error) {
      renderIncidents({ available: false, error: 'Incident page unavailable' });
    }
  }

  function mergeTimelineEntries(incoming) {
    const byId = new Map();
    for (const entry of [...timelineEntries, ...incoming]) {
      if (entry && entry.id != null && !byId.has(String(entry.id))) byId.set(String(entry.id), entry);
    }
    timelineEntries = [...byId.values()];
  }

  function safeEventId(value) {
    const candidate = String(value ?? '');
    return /^\d+$/.test(candidate) ? candidate : null;
  }

  function renderEventDetail(data) {
    const target = document.getElementById('eventDetail');
    if (!target) return;
    const state = document.getElementById('eventDetailState');
    if (!data.available || !data.event) {
      if (state) state.textContent = data.status === 'not_found' ? 'Not found' : 'Unavailable';
      target.innerHTML = `<div class="empty">${esc(data.error || 'Select an event to inspect its bounded detail.')}</div>`;
      return;
    }
    const event = data.event;
    if (state) state.textContent = 'Observed';
    const evidence = Array.isArray(event.evidence) ? event.evidence : [];
    target.innerHTML = `<dl class="event-detail-grid"><div><dt>ID</dt><dd>${esc(event.id)}</dd></div><div><dt>Event key</dt><dd>${esc(event.event_key)}</dd></div><div><dt>Occurred</dt><dd>${esc(event.occurred_at)}</dd></div><div><dt>Type</dt><dd>${esc(event.event_type)}</dd></div><div><dt>Severity</dt><dd>${esc(event.severity)}</dd></div><div><dt>Source</dt><dd>${esc(event.source)}</dd></div><div><dt>Resource</dt><dd>${esc(event.resource?.name || event.resource?.identifier || '—')}</dd></div><div><dt>Resource type</dt><dd>${esc(event.resource?.type || '—')}</dd></div><div><dt>Source run</dt><dd>${esc(event.source_run_id ?? '—')}</dd></div><div><dt>Previous run</dt><dd>${esc(event.previous_run_id ?? '—')}</dd></div><div><dt>Correlation</dt><dd>${esc(event.correlation_key || '—')}</dd></div></dl><p>${esc(event.title || '')}</p><p class="subtle">${esc(event.description || '')}</p><section class="event-evidence"><h4>Evidence</h4>${evidence.length ? evidence.map(item => `<article class="incident-event"><strong>${esc(item.code)}</strong><span>${esc(item.title)} · ${esc(item.description)}</span></article>`).join('') : '<div class="empty">No bounded evidence recorded.</div>'}</section>`;
  }

  async function loadEventDetail(eventId) {
    const safeId = safeEventId(eventId);
    eventDetailSerial += 1;
    if (safeId === null) {
      eventDetailRequest = null;
      renderEventDetail({ available: false, status: 'error', error: 'Event detail unavailable' });
      return;
    }
    const requestState = {
      serial: eventDetailSerial,
      incidentId: timelineIncidentId,
      generation: timelineGeneration,
      eventId: safeId,
    };
    eventDetailRequest = requestState;
    const isCurrentRequest = () => eventDetailRequest === requestState
      && timelineIncidentId === requestState.incidentId
      && timelineGeneration === requestState.generation
      && requestState.eventId === safeId;
    try {
      const response = await fetch(`/api/events/${safeId}`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (isCurrentRequest()) renderEventDetail(data);
    } catch (error) {
      if (isCurrentRequest()) renderEventDetail({ available: false, status: 'unavailable', error: 'Event detail unavailable' });
    } finally {
      if (isCurrentRequest()) eventDetailRequest = null;
    }
  }

  function renderTimeline() {
    const target = document.getElementById('incidentTimeline');
    if (!target) return;
    const entries = timelineEntries;
    const partial = timelinePartial;
    const state = timelineError ? `<div class="empty">${esc(timelineError)}</div>` : '';
    const content = entries.length
      ? `<div class="incident-timeline">${entries.map(entry => `<div class="incident-event"><time>${esc(entry.occurred_at)}</time><strong>${esc(entry.transition)}</strong><span>${esc(entry.previous_status || '—')} → ${esc(entry.current_status || '—')} · ${esc(entry.title)}</span>${entry.event_id == null ? '' : `<button type="button" class="evidence-link" data-event-id="${esc(entry.event_id)}">Event #${esc(entry.event_id)}</button>`}</div>`).join('')}</div>`
      : `<div class="empty">${esc(timelineError || 'No persisted timeline evidence available.')}</div>`;
    const continuation = timelineHasMore && typeof timelineCursor === 'string' && timelineCursor.length > 0
      ? `<div class="section-actions"><button type="button" id="timelineNext" class="range-button"${timelineInFlight ? ' disabled aria-busy="true"' : ''}>Next timeline page</button></div>`
      : '';
    const partialNotice = partial ? '<p class="subtle">Some persisted event evidence was unavailable; no timeline entries were inferred.</p>' : '';
    target.innerHTML = `${state}${content}${partialNotice}${continuation}`;
    target.querySelectorAll('[data-event-id]').forEach(button => {
      button.addEventListener('click', () => loadEventDetail(button.dataset.eventId));
    });
    const next = document.getElementById('timelineNext');
    if (next) next.addEventListener('click', () => loadTimeline(timelineIncidentId, { append: true }));
  }

  async function loadTimeline(incidentId, { append = false } = {}) {
    const targetId = String(incidentId ?? '');
    if (append && timelineIncidentId !== targetId) return;
    if (append && timelineInFlight) return;
    if (append && (!timelineHasMore || typeof timelineCursor !== 'string' || timelineCursor.length === 0)) return;
    if (!append) {
      timelineGeneration += 1;
      eventDetailRequest = null;
      timelineIncidentId = targetId;
      timelineCursor = null;
      timelineHasMore = false;
      timelinePartial = false;
      timelineEntries = [];
      timelineError = null;
      renderEventDetail({ available: false, status: 'idle', error: 'Select an event to inspect its bounded detail.' });
    }
    const requestState = { incidentId: targetId, generation: timelineGeneration, cursor: append ? timelineCursor : null };
    timelineInFlight = requestState;
    if (append) renderTimeline();
    const params = new URLSearchParams();
    params.set('limit', '50');
    if (append && typeof requestState.cursor === 'string' && requestState.cursor.length > 0) params.set('cursor', requestState.cursor);
    const isCurrentTimelineRequest = () => timelineInFlight === requestState && timelineIncidentId === requestState.incidentId && timelineGeneration === requestState.generation;
    try {
      const response = await fetch(`/api/incidents/${encodeURIComponent(targetId)}/timeline?${params.toString()}`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!isCurrentTimelineRequest()) return;
      const incoming = Array.isArray(data.entries) ? data.entries : [];
      if (!append) timelineEntries = [];
      mergeTimelineEntries(incoming);
      timelinePartial = timelinePartial || data.partial === true;
      timelineCursor = data.next_cursor;
      timelineHasMore = data.has_more === true && typeof data.next_cursor === 'string' && data.next_cursor.length > 0;
      timelineError = data.available ? null : (data.error || 'Incident timeline unavailable');
    } catch (error) {
      if (isCurrentTimelineRequest()) timelineError = append ? 'Next timeline page unavailable' : 'Incident timeline unavailable';
    } finally {
      if (isCurrentTimelineRequest()) {
        timelineInFlight = null;
        renderTimeline();
      }
    }
  }

  function renderHistory(data) {
    const target = document.getElementById('historyComparison');
    if (!target) return;
    if (!data.available) {
      target.innerHTML = `<div class="empty">${esc(data.error || 'History comparison unavailable.')}</div>`;
      return;
    }
    const changes = data.changes || [];
    target.innerHTML = `<div class="comparison-sides"><div><strong>Baseline</strong><span>${esc(data.baseline?.observed_at || 'Missing')}</span></div><div><strong>Current</strong><span>${esc(data.current?.observed_at || 'Missing')}</span></div></div><div class="table-wrap"><table><thead><tr><th>Field</th><th>Status</th><th>Before</th><th>After</th><th>Delta</th></tr></thead><tbody>${changes.map(item => `<tr><td>${esc(item.name)}</td><td>${esc(item.status)}</td><td>${esc(item.before ?? '—')}</td><td>${esc(item.after ?? '—')}</td><td>${esc(item.delta ?? '—')}</td></tr>`).join('')}</tbody></table></div>`;
  }

  return {
    loadIncidents: () => { incidentCursor = null; return loadIncidentPage(); },
    renderIncidents,
    renderHistory,
    loadTimeline,
    loadEventDetail,
    renderTimeline,
    renderEventDetail,
  };
}

export { createEvidenceController };

if (typeof window !== 'undefined') window.createEvidenceController = createEvidenceController;
