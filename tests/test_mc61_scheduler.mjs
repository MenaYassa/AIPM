import assert from 'node:assert/strict';
import test from 'node:test';

import { MissionControlScheduler } from '../src/aipm/dashboard/static/mission-control-scheduler.js';

function harness() {
  let nextId = 1;
  const timers = new Map();
  let visibility = 'visible';
  const scheduler = new MissionControlScheduler({
    setTimer(callback, delay) {
      const id = nextId++;
      timers.set(id, { callback: () => { timers.delete(id); callback(); }, delay });
      return id;
    },
    clearTimer(id) {
      timers.delete(id);
    },
    now: () => 1000,
    visibility: () => visibility,
    maxBackoffMs: 1000,
  });
  return {
    scheduler,
    timers,
    setVisibility(value) {
      visibility = value;
    },
    visibilityTarget: {
      listeners: new Set(),
      addEventListener(_name, callback) { this.listeners.add(callback); },
      removeEventListener(_name, callback) { this.listeners.delete(callback); },
      emit() { for (const callback of this.listeners) callback(); },
    },
  };
}

test('register rejects duplicates and creates one timer per resource', async () => {
  const { scheduler, timers } = harness();
  let calls = 0;
  scheduler.register('overview', async () => { calls += 1; }, { intervalMs: 15000, immediate: false });
  assert.throws(() => scheduler.register('overview', async () => {}, { intervalMs: 15000 }), /already registered/);
  assert.equal(timers.size, 1);
  const timer = [...timers.values()][0];
  timer.callback();
  await Promise.resolve();
  assert.equal(calls, 1);
  assert.equal(timers.size, 1);
});

test('refresh does not overlap an in-flight request or create a second timer', async () => {
  const { scheduler, timers } = harness();
  let resolveRequest;
  let calls = 0;
  const pending = new Promise((resolve) => { resolveRequest = resolve; });
  scheduler.register('events', async () => { calls += 1; await pending; }, { intervalMs: 15000, immediate: false });
  const first = scheduler.refresh('events');
  const second = await scheduler.refresh('events');
  assert.equal(second, false);
  assert.equal(calls, 1);
  assert.equal(timers.size, 1);
  resolveRequest();
  assert.equal(await first, true);
  assert.equal(timers.size, 1);
});

test('manual refresh uses the existing resource and does not add timers', async () => {
  const { scheduler, timers } = harness();
  let calls = 0;
  scheduler.register('history', async () => { calls += 1; }, { intervalMs: 60000, immediate: false });
  assert.equal(await scheduler.refresh('history'), true);
  assert.equal(calls, 1);
  assert.equal(timers.size, 1);
  const existingTimer = [...timers.keys()][0];
  assert.equal(await scheduler.refresh('history'), true);
  assert.equal(calls, 2);
  assert.equal(timers.size, 1);
  assert.equal(timers.has(existingTimer), true);
});

test('visibility pause cancels timers and resume refreshes without duplicates', async () => {
  const { scheduler, timers, setVisibility, visibilityTarget } = harness();
  let calls = 0;
  scheduler.register('services', async () => { calls += 1; }, { intervalMs: 15000, immediate: false });
  const stop = scheduler.startVisibilityHandling(visibilityTarget);
  assert.equal(timers.size, 1);
  setVisibility('hidden');
  visibilityTarget.emit();
  assert.equal(timers.size, 0);
  setVisibility('visible');
  visibilityTarget.emit();
  await Promise.resolve();
  assert.equal(calls, 1);
  assert.equal(timers.size, 1);
  stop();
  assert.equal(visibilityTarget.listeners.size, 0);
});

test('failed requests use bounded retry delay and cleanup cancels timers', async () => {
  const { scheduler, timers } = harness();
  scheduler.register('notifications', async () => { throw new Error('temporary'); }, { intervalMs: 100, immediate: false });
  await scheduler.refresh('notifications');
  assert.equal([...timers.values()][0].delay, 100);
  timers.values().next().value.callback();
  await Promise.resolve();
  assert.equal([...timers.values()][0].delay, 200);
  scheduler.cleanup();
  assert.equal(timers.size, 0);
});
