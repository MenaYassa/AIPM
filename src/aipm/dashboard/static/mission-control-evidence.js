export function createEvidenceController({ escapeHtml }) {
  const esc = escapeHtml || (value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char])));
  let incidentCursor = null;

  function renderIncidents(data) {
    const target = document.getElementById('incidentPage');
    if (!target) return;
    const items = data.incidents || [];
    const state = document.getElementById('incidentPageState');
    if (state) state.textContent = data.available ? `${items.length} shown` : 'Unavailable';
    target.innerHTML = data.available && items.length
      ? items.map(item => `<article class="incident-card"><h4>${esc(item.title)}</h4><div class="incident-meta"><span>${esc(item.severity)}</span><span>${esc(item.status)}</span><span>${esc(item.resource?.name || item.resource?.identifier || 'resource')}</span><span>${esc(item.updated_at || item.started_at || '—')}</span></div><p class="subtle">${esc(item.summary)}</p><button class="evidence-link" data-incident-id="${esc(item.id)}">View persisted timeline</button></article>`).join('')
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

  async function loadTimeline(incidentId) {
    const target = document.getElementById('incidentTimeline');
    if (!target) return;
    try {
      const response = await fetch(`/api/incidents/${encodeURIComponent(incidentId)}/timeline?limit=50`, { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      target.innerHTML = data.available && data.entries?.length
        ? `<div class="incident-timeline">${data.entries.map(entry => `<div class="incident-event"><time>${esc(entry.occurred_at)}</time><strong>${esc(entry.transition)}</strong><span>${esc(entry.previous_status || '—')} → ${esc(entry.current_status || '—')} · ${esc(entry.title)}</span>${entry.event_id == null ? '' : `<a class="evidence-link" href="#/dashboard">Event #${esc(entry.event_id)}</a>`}</div>`).join('')}</div>${data.partial ? '<p class="subtle">Some persisted event evidence was unavailable; no timeline entries were inferred.</p>' : ''}`
        : `<div class="empty">${esc(data.error || 'No persisted timeline evidence available.')}</div>`;
    } catch (error) {
      target.innerHTML = '<div class="empty">Incident timeline unavailable.</div>';
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
  };
}
