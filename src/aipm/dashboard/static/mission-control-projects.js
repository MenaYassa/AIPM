'use strict';

const escDefault = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

export function createProjectController({scheduler, stateClass, escapeHtml = escDefault}) {
  const $ = id => document.getElementById(id);
  let selectedId = null;
  let latest = null;

  const badge = (label, state) => `<span class="badge ${stateClass(state || 'unknown')}">${escapeHtml(label)}</span>`;
  const stateLabel = state => String(state || 'unknown').replaceAll('_', ' ');
  const healthCounts = health => {
    const counts = health?.counts || {};
    const total = Number.isFinite(Number(counts.total)) ? Number(counts.total) : 0;
    const running = Number(counts.running || 0);
    const healthy = Number(counts.healthy || 0);
    const missing = Number(counts.missing_health_check || 0);
    const unhealthy = Number(counts.unhealthy || 0);
    const unknown = Math.max(0, Number(counts.unknown ?? (total - healthy - missing - unhealthy)) || 0);
    return {total, running, healthy, missing, unknown};
  };
  const healthEvidenceHtml = health => {
    const counts = healthCounts(health);
    if (!counts.total) return '<div class="health-line health-line-warning">⚠ No runtime health evidence available</div>';
    return `<div class="health-line">✓ Running containers: ${counts.running}/${counts.total}</div><div class="health-line">✓ Healthy containers: ${counts.healthy}/${counts.total}</div><div class="health-line ${counts.missing ? 'health-line-warning' : ''}">${counts.missing ? '⚠' : '✓'} Missing health checks: ${counts.missing}/${counts.total}</div><div class="health-line ${counts.unknown ? 'health-line-warning' : ''}">${counts.unknown ? '⚠' : '✓'} Unknown: ${counts.unknown}</div>`;
  };
  const healthSummary = health => {
    const counts = healthCounts(health);
    return counts.total ? `${counts.running}/${counts.total} running · ${counts.healthy}/${counts.total} healthy · ${counts.missing}/${counts.total} missing health checks · ${counts.unknown} unknown` : 'No runtime health evidence available';
  };

  function projectCard(project, local = false) {
    const health = project.health || {};
    const freshness = project.freshness || {};
    const role = project.association_role || (local ? 'local_candidate' : 'application');
    const explanation = project.association_explanation || (local ? 'Local project discovered without a trustworthy runtime association.' : 'Runtime application observed.');
    const runtimeRole = ['associated_local', 'application', 'runtime_only', 'ungrouped'].includes(role);
    const summary = role === 'filtered_candidate' || !runtimeRole ? explanation : (health.summary || explanation);
    const explanationText = runtimeRole ? healthSummary(health) : summary;
    return `<button class="project-card ${local ? 'local-candidate-card' : ''}" type="button" data-project-id="${escapeHtml(project.id)}"><div class="project-card-head"><div><strong>${escapeHtml(project.display_name)}</strong><span>${escapeHtml(role)} · ${escapeHtml(project.confidence)}</span></div>${badge(health.status || 'unknown', health.status || 'unknown')}</div><div class="project-card-meta"><span>${project.component_count || 0} components</span><span>${project.runtime?.running || 0} running</span><span>${escapeHtml(stateLabel(freshness.state || freshness.status))}</span></div><p>${escapeHtml(explanationText)}</p></button>`;
  }

  function renderInventory(data) {
    latest = data;
    const observation = data.observation || {state: 'unknown'};
    $('projectInventoryState').textContent = stateLabel(observation.state);
    $('projectInventoryState').className = `badge ${stateClass(observation.state || 'unknown')}`;
    $('projectInventoryAge').textContent = observation.age_seconds == null ? (observation.state === 'never_sampled' ? 'Never sampled' : 'Freshness unavailable') : `Sample age ${observation.age_seconds}s`;
    const allProjects = data.projects || [];
    const applications = allProjects.filter(project => ['associated_local', 'application'].includes(project.association_role));
    const runtimeGroups = allProjects.filter(project => ['runtime_only', 'ungrouped'].includes(project.association_role));
    const localProjects = data.local_candidates || [];
    const filteredCandidates = data.filtered_candidates || [];
    $('projectInventoryCount').textContent = `${applications.length} application${applications.length === 1 ? '' : 's'}`;
    $('runtimeGroupCount').textContent = `${runtimeGroups.length} group${runtimeGroups.length === 1 ? '' : 's'}`;
    $('localProjectCount').textContent = `${localProjects.length} project${localProjects.length === 1 ? '' : 's'}`;
    $('filteredCandidateCount').textContent = `${filteredCandidates.length} candidate${filteredCandidates.length === 1 ? '' : 's'}`;
    $('projectInventoryErrors').innerHTML = (data.source_errors || []).map(error => `<span>${escapeHtml(error)}</span>`).join('');
    const emptyMessage = (message) => `<div class="empty">${escapeHtml(data.error || ((observation.state === 'unavailable' || observation.state === 'never_sampled') ? 'Application inventory is not available yet.' : message))}</div>`;
    $('projectCards').innerHTML = applications.length ? applications.map(project => projectCard(project)).join('') : emptyMessage('No runtime-backed applications were discovered.');
    $('runtimeGroupCards').innerHTML = runtimeGroups.length ? runtimeGroups.map(project => projectCard(project)).join('') : '<div class="empty">No runtime-only groups in this observation.</div>';
    $('localProjectCards').innerHTML = localProjects.length ? localProjects.map(project => projectCard(project, true)).join('') : '<div class="empty">No local projects in this observation.</div>';
    $('filteredCandidateCards').innerHTML = filteredCandidates.length ? filteredCandidates.map(project => projectCard(project, true)).join('') : '<div class="empty">No filtered candidates in this observation.</div>';
    document.querySelectorAll('[data-project-id]').forEach(button => button.addEventListener('click', () => selectProject(button.dataset.projectId)));
    const selectable = applications.concat(runtimeGroups, localProjects, filteredCandidates);
    if (selectedId && selectable.some(project => project.id === selectedId)) selectProject(selectedId);
    else if (!selectedId && applications[0]) selectProject(applications[0].id);
    else if (!selectedId && runtimeGroups[0]) selectProject(runtimeGroups[0].id);
    else if (!selectedId && localProjects[0]) selectProject(localProjects[0].id);
    else if (!selectedId && filteredCandidates[0]) selectProject(filteredCandidates[0].id);
    else if (!selectable.length) clearDetail('Select a project when an observation becomes available.');
  }

  function clearDetail(message) {
    $('projectDetailState').textContent = 'Unknown';
    $('projectDetailState').className = 'badge unknown';
    $('projectDetail').innerHTML = `<div class="empty">${escapeHtml(message)}</div>`;
  }

  function renderDetail(data, healthData, containersData) {
    const project = data.project;
    if (!project) return clearDetail(data.error || 'Project detail unavailable.');
    const health = healthData.health || project.health || {};
    const components = containersData.containers || project.components || [];
    $('projectDetailState').textContent = stateLabel(health.status || 'unknown');
    $('projectDetailState').className = `badge ${stateClass(health.status || 'unknown')}`;
    const evidence = (health.evidence || project.evidence || []).map(item => `<div class="evidence-row"><span class="badge ${stateClass(item.severity === 'warning' ? 'warning' : 'unknown')}">${escapeHtml(item.code)}</span><span>${escapeHtml(item.message)}</span></div>`).join('');
    const tree = components.length ? `<div class="component-tree">${components.map(item => `<div class="component-row"><div><strong>${escapeHtml(item.service_name || item.name)}</strong><span>${escapeHtml(item.name)} · ${escapeHtml(item.image || 'image unavailable')}</span></div><div>${badge(item.state || 'unknown', item.state === 'running' ? 'healthy' : item.state === 'exited' ? 'critical' : 'warning')} ${item.health ? badge(item.health, item.health === 'healthy' ? 'healthy' : 'critical') : '<span class="subtle">health check missing</span>'}</div></div>`).join('')}</div>` : '<div class="empty">No runtime components are associated with this project.</div>';
    $('projectDetail').innerHTML = `<div class="detail-title"><div><div class="eyebrow">Application detail</div><h3>${escapeHtml(project.display_name)}</h3><p>${escapeHtml(project.source)} · ${escapeHtml(project.confidence)} association · ${escapeHtml(project.freshness?.state || project.freshness?.status || 'unknown')}</p></div>${badge(health.status || 'unknown', health.status || 'unknown')}</div><div class="detail-grid"><div><span class="metric-label">Components</span><strong>${components.length}</strong></div><div><span class="metric-label">Running</span><strong>${health.counts?.running ?? project.runtime?.running ?? 0}</strong></div><div><span class="metric-label">Healthy</span><strong>${health.counts?.healthy ?? 0}</strong></div><div><span class="metric-label">Missing health checks</span><strong>${health.counts?.missing_health_check ?? 0}</strong></div></div><section class="health-evidence"><h4>Health evidence</h4>${healthEvidenceHtml(health)}</section><div class="detail-columns"><div><h4>Component tree</h4>${tree}</div><div><h4>Raw evidence</h4><div class="evidence-list">${evidence || '<div class="empty">No additional evidence.</div>'}</div></div></div><div class="posture-grid"><div><h4>Git posture</h4><p>${escapeHtml(project.git?.status || 'unavailable')} · branch ${escapeHtml(project.git?.branch || 'unknown')}</p><span class="subtle">Ahead ${project.git?.ahead ?? '—'} · behind ${project.git?.behind ?? '—'} · conflicts ${project.git?.conflicted ? 'present' : 'none observed'}</span></div><div><h4>Compose posture</h4><p>${escapeHtml(project.compose?.status || 'unavailable')}</p><span class="subtle">${(project.compose?.file_names || []).map(escapeHtml).join(', ') || 'No Compose file metadata available'}</span></div></div>`;
  }

  async function selectProject(projectId) {
    if (!projectId) return;
    selectedId = projectId;
    document.querySelectorAll('[data-project-id]').forEach(button => button.classList.toggle('selected', button.dataset.projectId === projectId));
    try {
      const responses = await Promise.all([
        fetch(`/api/projects/${encodeURIComponent(projectId)}`, {cache: 'no-store'}),
        fetch(`/api/projects/${encodeURIComponent(projectId)}/health`, {cache: 'no-store'}),
        fetch(`/api/projects/${encodeURIComponent(projectId)}/containers`, {cache: 'no-store'})
      ]);
      if (responses.some(response => !response.ok)) throw new Error('Project detail unavailable');
      renderDetail(await responses[0].json(), await responses[1].json(), await responses[2].json());
    } catch (error) {
      clearDetail('Project detail is unavailable; unaffected inventory observations remain visible.');
    }
  }

  async function load() {
    try {
      const response = await fetch('/api/projects?scope=applications&limit=200', {cache: 'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      renderInventory(await response.json());
    } catch (error) {
      renderInventory({available: false, status: 'error', error: 'Application inventory unavailable', observation: {state: 'error'}, projects: [], local_candidates: [], filtered_candidates: [], source_errors: ['Application inventory unavailable']});
    }
  }

  return {load, selectProject, latest: () => latest, cleanup: () => {selectedId = null;}};
}
