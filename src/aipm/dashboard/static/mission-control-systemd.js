'use strict';

const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

export function createSystemdController({
  fetchImpl = fetch,
  documentRef = document,
  stateClass = state => String(state || 'unknown'),
  escapeHtml = esc,
} = {}) {
  let latest = null;
  let selectedId = null;

  const $ = id => documentRef.getElementById(id);
  const observation = data => data?.observation || {state: 'unknown', available: false};

  function renderObservation(data) {
    const meta = observation(data);
    const state = meta.state || 'unknown';
    const badge = $('systemdObservationState');
    if (badge) {
      badge.textContent = state.replaceAll('_', ' ');
      badge.className = `badge ${stateClass(state)}`;
    }
    const age = $('systemdObservationAge');
    if (age) age.textContent = meta.age_seconds == null ? (state === 'never_sampled' ? 'Never sampled' : 'Observation age unavailable') : `Observed ${meta.age_seconds}s ago`;
  }

  function renderUnits(data) {
    const units = data?.units || [];
    const target = $('systemdUnits');
    const count = $('systemdUnitCount');
    if (count) count.textContent = `${units.length} allow-listed`;
    if (!target) return;
    if (!units.length) {
      target.innerHTML = `<div class="empty">${escapeHtml(data?.errors?.[0]?.message || 'No allow-listed Systemd units observed.')}</div>`;
      return;
    }
    target.innerHTML = units.map(unit => {
      const state = unit.observation_state || 'unknown';
      const status = unit.status || 'unknown';
      return `<button class="systemd-unit-row" type="button" data-systemd-unit="${escapeHtml(unit.id)}"><span><strong>${escapeHtml(unit.display_name || unit.id)}</strong><small>${escapeHtml(unit.load_state || 'load unavailable')} · ${escapeHtml(unit.sub_state || 'sub-state unavailable')}</small></span><span class="badge ${stateClass(status === 'unavailable' ? state : status)}">${escapeHtml(status)}</span><span>${unit.enabled == null ? 'Enablement unknown' : unit.enabled ? 'Enabled' : 'Disabled'}</span></button>`;
    }).join('');
    target.querySelectorAll('[data-systemd-unit]').forEach(button => button.addEventListener('click', () => selectUnit(button.dataset.systemdUnit)));
  }

  function renderDetail(data) {
    const target = $('systemdDetail');
    if (!target) return;
    const unit = data?.unit;
    if (!unit) {
      target.innerHTML = `<div class="empty">Select an allow-listed unit to inspect its safe observation.</div>`;
      return;
    }
    const evidence = unit.evidence?.length ? `<div class="systemd-evidence">${unit.evidence.map(item => `<div>${escapeHtml(item)}</div>`).join('')}</div>` : '<div class="subtle">No additional evidence.</div>';
    target.innerHTML = `<div class="systemd-detail-title"><strong>${escapeHtml(unit.display_name || unit.id)}</strong><span class="badge ${stateClass(unit.status || 'unknown')}">${escapeHtml(unit.status || 'unknown')}</span></div><dl class="systemd-detail-grid"><div><dt>Load state</dt><dd>${escapeHtml(unit.load_state || '—')}</dd></div><div><dt>Active state</dt><dd>${escapeHtml(unit.active_state || '—')}</dd></div><div><dt>Sub-state</dt><dd>${escapeHtml(unit.sub_state || '—')}</dd></div><div><dt>Enablement</dt><dd>${unit.enabled == null ? 'Unknown' : unit.enabled ? 'Enabled' : 'Disabled'}</dd></div></dl><h4>Health / status</h4><p class="subtle">${escapeHtml(unit.status || 'unknown')} · ${escapeHtml(unit.observation_state || 'unknown')}</p><h4>Evidence</h4>${evidence}`;
  }

  async function selectUnit(id) {
    if (!id) return;
    selectedId = id;
    try {
      const response = await fetchImpl(`/api/systemd/units/${encodeURIComponent(id)}`, {cache: 'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderDetail(await response.json());
    } catch (_error) {
      renderDetail({unit: null, errors: [{message: 'Systemd unit detail unavailable'}]});
    }
  }

  async function load() {
    try {
      const response = await fetchImpl('/api/systemd/units?limit=20', {cache: 'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      latest = await response.json();
      renderObservation(latest);
      renderUnits(latest);
      if (selectedId) await selectUnit(selectedId);
    } catch (_error) {
      latest = {observation: {state: 'error', available: false}, units: [], errors: [{message: 'Systemd observation unavailable'}]};
      renderObservation(latest);
      renderUnits(latest);
      renderDetail({unit: null});
    }
  }

  return {load, selectUnit, latest: () => latest, cleanup: () => {selectedId = null;}};
}
