import assert from 'node:assert/strict';
import test from 'node:test';

import { NAV_ITEMS, bindSidebar, createShellRouter, normalizeRoute, routeHash } from '../src/aipm/dashboard/static/mission-control-shell.js';

function classList() {
  const values = new Set();
  return {
    values,
    toggle(name, force) {
      const next = force === undefined ? !values.has(name) : force;
      if (next) values.add(name); else values.delete(name);
      return next;
    },
    contains(name) { return values.has(name); },
    add(name) { values.add(name); },
    remove(name) { values.delete(name); },
  };
}

function element(route) {
  const listeners = new Map();
  return {
    dataset: route ? { route } : { view: route },
    classList: classList(),
    hidden: false,
    attributes: new Map(),
    listeners,
    setAttribute(name, value) { this.attributes.set(name, String(value)); },
    removeAttribute(name) { this.attributes.delete(name); },
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name) { listeners.delete(name); },
    click() { listeners.get('click')?.(); },
  };
}

function fakeWindow(hash = '') {
  const listeners = new Map();
  return {
    location: { hash },
    history: { replaceState(_state, _title, value) { this.last = value; } },
    listeners,
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name) { listeners.delete(name); },
    matchMedia() { return { matches: true }; },
    emit(name) { listeners.get(name)?.(); },
  };
}

test('navigation inventory and route helpers are explicit and safe', () => {
  assert.deepEqual(NAV_ITEMS.map(([route]) => route), [
    'dashboard', 'server', 'docker', 'projects', 'systemd', 'logs', 'incidents', 'history', 'notifications', 'settings', 'ai-agent',
  ]);
  assert.equal(normalizeRoute(''), 'dashboard');
  assert.equal(normalizeRoute('#/history?range=24h'), 'history');
  assert.equal(normalizeRoute('#/not-a-view'), 'dashboard');
  assert.equal(routeHash('not-a-view'), '#/dashboard');
  assert.equal(routeHash('incidents'), '#/incidents');
});

test('router falls back to Dashboard and updates selected view state', () => {
  const windowRef = fakeWindow('#/unknown');
  const links = NAV_ITEMS.map(([route]) => element(route));
  const views = NAV_ITEMS.map(([route]) => ({ ...element(), dataset: { view: route } }));
  const header = { textContent: '' };
  const subtitle = { textContent: '' };
  const seen = [];
  const router = createShellRouter({ windowRef, links, views, header, subtitle, onRoute: (route) => seen.push(route) });

  assert.equal(windowRef.history.last, '#/dashboard');
  assert.equal(links[0].classList.contains('active'), true);
  assert.equal(views[0].hidden, false);
  assert.equal(views[1].hidden, true);
  assert.equal(header.textContent, 'Dashboard');
  assert.deepEqual(seen, ['dashboard']);

  windowRef.location.hash = '#/server';
  windowRef.emit('hashchange');
  assert.equal(links[1].classList.contains('active'), true);
  assert.equal(links[0].classList.contains('active'), false);
  assert.equal(views[1].hidden, false);
  assert.equal(views[0].hidden, true);
  assert.equal(header.textContent, 'Server');
  router.navigate('history');
  assert.equal(windowRef.location.hash, '#/history');
  router.destroy();
  assert.equal(windowRef.listeners.has('hashchange'), false);
});

test('sidebar toggles on mobile and cleanup removes listeners', () => {
  const windowRef = fakeWindow();
  const toggle = element();
  const sidebar = element();
  const links = [element('dashboard'), element('server')];
  const stop = bindSidebar({ windowRef, toggle, sidebar, links });

  toggle.click();
  assert.equal(sidebar.classList.contains('open'), true);
  assert.equal(toggle.attributes.get('aria-expanded'), 'true');
  links[1].click();
  assert.equal(sidebar.classList.contains('open'), false);
  assert.equal(toggle.attributes.get('aria-expanded'), 'false');
  stop();
  assert.equal(toggle.listeners.has('click'), false);
  assert.equal(links[0].listeners.has('click'), false);
});
