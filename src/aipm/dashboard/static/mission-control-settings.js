export function createSettingsController({ stateClass, escapeHtml }) {
  const esc = escapeHtml || (value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char])));

  function setState(label, state) {
    const badge = document.getElementById('settingsObservationState');
    if (!badge) return;
    badge.textContent = label || state || 'unknown';
    badge.className = `badge ${(stateClass || (value => value || 'unknown'))(state || 'unknown')}`;
  }

  function value(value, fallback = '—') {
    return value == null || value === '' ? fallback : esc(value);
  }

  function renderRows(items) {
    return items.map(([label, item]) => `<div class="signal"><span>${esc(label)}</span><strong>${value(item)}</strong></div>`).join('');
  }

  function render(data) {
    const root = document.getElementById('settingsPosture');
    if (!root) return;
    if (!data || !data.available) {
      setState('Unavailable', data?.status || 'unavailable');
      root.innerHTML = `<div class="empty">${esc(data?.error || 'Settings posture unavailable.')}</div>`;
      return;
    }
    const application = data.application || {};
    const deployment = data.deployment || {};
    const readOnly = data.read_only || {};
    const telemetry = data.telemetry || {};
    const mc3 = data.mc3 || {};
    const notifications = data.notifications || {};
    const audit = notifications.audit || {};
    const auditAvailable = audit.availability === 'observed';
    const capabilities = data.capabilities || [];
    setState('Observed', data.status || 'ok');
    document.getElementById('settingsGeneratedAt').textContent = data.generated_at ? new Date(data.generated_at).toLocaleString() : '—';
    document.getElementById('settingsApplication').innerHTML = renderRows([
      ['Version', application.version],
      ['Commit', application.commit || 'Unknown'],
      ['State', application.state],
    ]);
    document.getElementById('settingsDeployment').innerHTML = renderRows([
      ['Binding', deployment.binding],
      ['Public ingress', deployment.public_ingress],
      ['Permanent service', deployment.permanent_service],
    ]);
    document.getElementById('settingsReadOnly').innerHTML = renderRows([
      ['SQLite', readOnly.sqlite_mode],
      ['Query-only', String(readOnly.query_only)],
      ['Filesystem write boundary', readOnly.filesystem_write_boundary],
      ['Schema mutation', readOnly.schema_mutation],
      ['Checkpointing', readOnly.checkpointing],
    ]);
    document.getElementById('settingsServices').innerHTML = renderRows([
      ['Telemetry', `${telemetry.enabled ? 'enabled' : 'disabled'} · ${telemetry.state || 'unknown'} · ${telemetry.interval_seconds || 0}s`],
      ['MC-3 / Events', `${mc3.enabled ? 'enabled' : 'disabled'} · ${mc3.state || 'unknown'} · ${mc3.interval_seconds || 0}s`],
    ]);
    document.getElementById('settingsCapabilities').innerHTML = capabilities.length
      ? capabilities.map(item => `<div class="incident-card"><div class="incident-meta"><strong>${esc(item.name)}</strong><span class="badge ${(stateClass || (state => state || 'unknown'))(item.state || 'unknown')}">${esc(item.state || 'unknown')}</span></div><p class="subtle">${item.available ? 'Observation capability available' : 'Observation capability not observed'}</p></div>`).join('')
      : '<div class="empty">Capability posture unavailable.</div>';
    document.getElementById('settingsNotifications').innerHTML = renderRows([
      ['Notifications', notifications.enabled ? 'enabled' : 'disabled'],
      ['Provider', notifications.provider_state],
      ['Audit status', auditAvailable ? 'Observed' : 'Unavailable'],
      ['Configured channels', notifications.configured_channel_count],
      ['Enabled channels', notifications.enabled_channel_count],
      ['Configured policies', notifications.configured_policy_count],
      ['Enabled policies', notifications.enabled_policy_count],
      ['Pending', auditAvailable ? audit.pending : null],
      ['Sending', auditAvailable ? audit.sending : null],
      ['Sent', auditAvailable ? audit.sent : null],
      ['Failed', auditAvailable ? audit.failed : null],
      ['Unknown', auditAvailable ? audit.unknown : null],
      ['Suppressed', auditAvailable ? audit.suppressed : null],
      ['Retry exhaustion', auditAvailable ? audit.retry_exhaustion_count : null],
      ['Recent latency', auditAvailable && audit.recent_delivery_latency_seconds != null ? `${audit.recent_delivery_latency_seconds}s` : '—'],
      ['Schema version', audit.schema_version == null ? 'Unknown' : audit.schema_version],
    ]);
  }

  async function load() {
    const response = await fetch('/api/settings/posture', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  }

  return { load, render };
}
