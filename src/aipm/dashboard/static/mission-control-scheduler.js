export class MissionControlScheduler {
  constructor({
    setTimer = window.setTimeout.bind(window),
    clearTimer = window.clearTimeout.bind(window),
    now = () => Date.now(),
    visibility = () => document.visibilityState,
    maxBackoffMs = 120000,
  } = {}) {
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.now = now;
    this.visibility = visibility;
    this.maxBackoffMs = maxBackoffMs;
    this.resources = new Map();
  }

  register(name, loader, { intervalMs, immediate = true, maxRetries = 3 } = {}) {
    if (!name || typeof loader !== 'function') throw new TypeError('resource name and loader are required');
    if (!Number.isFinite(intervalMs) || intervalMs <= 0) throw new RangeError('intervalMs must be positive');
    if (this.resources.has(name)) throw new Error(`resource already registered: ${name}`);
    const resource = {
      name,
      loader,
      intervalMs,
      maxRetries: Math.max(0, Math.floor(maxRetries)),
      timer: null,
      running: false,
      stopped: false,
      paused: false,
      failures: 0,
      lastStartedAt: null,
    };
    this.resources.set(name, resource);
    if (immediate) void this.refresh(name);
    else this.#schedule(resource, intervalMs);
    return () => this.unregister(name);
  }

  async refresh(name) {
    const resource = this.resources.get(name);
    if (!resource || resource.stopped || resource.running) return false;
    resource.running = true;
    resource.lastStartedAt = this.now();
    try {
      await resource.loader();
      resource.failures = 0;
    } catch (error) {
      resource.failures = Math.min(resource.failures + 1, resource.maxRetries + 1);
      if (typeof resource.onError === 'function') resource.onError(error);
    } finally {
      resource.running = false;
      if (!resource.stopped && !resource.paused) this.#schedule(resource, this.#delay(resource));
    }
    return true;
  }

  pause() {
    for (const resource of this.resources.values()) {
      resource.paused = true;
      if (resource.timer !== null) {
        this.clearTimer(resource.timer);
        resource.timer = null;
      }
    }
  }

  resume() {
    for (const resource of this.resources.values()) {
      if (resource.stopped) continue;
      resource.paused = false;
      if (!resource.running && resource.timer === null) void this.refresh(resource.name);
    }
  }

  startVisibilityHandling(target = document) {
    const handler = () => {
      if (this.visibility() === 'hidden') this.pause();
      else this.resume();
    };
    target.addEventListener('visibilitychange', handler);
    return () => target.removeEventListener('visibilitychange', handler);
  }

  unregister(name) {
    const resource = this.resources.get(name);
    if (!resource) return false;
    resource.stopped = true;
    if (resource.timer !== null) this.clearTimer(resource.timer);
    resource.timer = null;
    this.resources.delete(name);
    return true;
  }

  cleanup() {
    for (const name of this.resources.keys()) this.unregister(name);
  }

  #delay(resource) {
    if (resource.failures === 0) return resource.intervalMs;
    return Math.min(this.maxBackoffMs, resource.intervalMs * (2 ** Math.min(resource.failures - 1, 4)));
  }

  #schedule(resource, delay) {
    if (resource.stopped || resource.paused || resource.timer !== null) return;
    resource.timer = this.setTimer(() => {
      resource.timer = null;
      void this.refresh(resource.name);
    }, delay);
  }
}
