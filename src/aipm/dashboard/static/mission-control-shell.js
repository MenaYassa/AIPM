export const NAV_ITEMS = Object.freeze([
  ['dashboard', 'Dashboard', 'Overview and live posture'],
  ['server', 'Server', 'Host capacity and identity'],
  ['docker', 'Docker', 'Containers and resources'],
  ['projects', 'Projects', 'Git and Compose inventory'],
  ['systemd', 'Systemd', 'Service observations'],
  ['logs', 'Logs', 'Bounded operational logs'],
  ['incidents', 'Incidents', 'MC-3 Incident Room'],
  ['history', 'History', 'Telemetry trends'],
  ['notifications', 'Notifications', 'Safety and audit'],
  ['settings', 'Settings', 'Effective posture'],
  ['ai-agent', 'AI Agent', 'Future advisor'],
]);

const ROUTES = new Set(NAV_ITEMS.map(([route]) => route));

export function normalizeRoute(hash = '') {
  const route = String(hash).replace(/^#\/?/, '').split('?')[0].replace(/\/$/, '') || 'dashboard';
  return ROUTES.has(route) ? route : 'dashboard';
}

export function routeHash(route) {
  return `#/${normalizeRoute(route)}`;
}

export function createShellRouter({
  windowRef = window,
  links = [],
  views = [],
  header,
  subtitle,
  onRoute = () => {},
} = {}) {
  const apply = () => {
    const route = normalizeRoute(windowRef.location.hash);
    const desiredHash = routeHash(route);
    if (windowRef.location.hash !== desiredHash) {
      windowRef.history.replaceState({}, '', desiredHash);
    }
    for (const link of links) {
      const selected = link.dataset.route === route;
      link.classList.toggle('active', selected);
      if (selected) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    for (const view of views) {
      const selected = view.dataset.view === route;
      view.hidden = !selected;
      view.setAttribute('aria-hidden', String(!selected));
    }
    const item = NAV_ITEMS.find(([key]) => key === route) || NAV_ITEMS[0];
    if (header) header.textContent = item[1];
    if (subtitle) subtitle.textContent = item[2];
    onRoute(route);
    return route;
  };
  const onHashChange = () => apply();
  windowRef.addEventListener('hashchange', onHashChange);
  apply();
  return {
    apply,
    navigate(route) {
      windowRef.location.hash = routeHash(route);
    },
    destroy() {
      windowRef.removeEventListener('hashchange', onHashChange);
    },
  };
}

export function bindSidebar({
  windowRef = window,
  toggle,
  sidebar,
  links = [],
} = {}) {
  if (!toggle || !sidebar) return () => {};
  const close = () => {
    sidebar.classList.remove('open');
    toggle.setAttribute('aria-expanded', 'false');
  };
  const onToggle = () => {
    const open = sidebar.classList.toggle('open');
    toggle.setAttribute('aria-expanded', String(open));
  };
  const onLink = () => {
    if (windowRef.matchMedia && windowRef.matchMedia('(max-width: 820px)').matches) close();
  };
  toggle.addEventListener('click', onToggle);
  for (const link of links) link.addEventListener('click', onLink);
  return () => {
    toggle.removeEventListener('click', onToggle);
    for (const link of links) link.removeEventListener('click', onLink);
    close();
  };
}
