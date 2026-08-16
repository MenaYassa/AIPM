from __future__ import annotations

import signal
import threading
import time
from typing import Any, Callable

from aipm.models.config import TelemetryConfig
from aipm.models.telemetry_sampling import SlowTaskState
from aipm.services.telemetry.sampler import TelemetrySampler


class _SingleFlightSlot:
    def __init__(self, name: str, logger: Any | None = None) -> None:
        self.name = name
        self.logger = logger
        self.running = False
        self.last_started_at: float | None = None
        self.last_finished_at: float | None = None
        self.last_duration_ms: int | None = None
        self.last_status = "never_sampled"
        self.skipped_count = 0
        self.error: Exception | None = None
        self._timed_out = False
        self._lock = threading.Lock()

    def start(self, work: Callable[[], Any], *, timeout_seconds: int | None = None) -> bool:
        with self._lock:
            if self.running:
                self.skipped_count += 1
                return False
            self.running = True
            self.last_started_at = time.monotonic()
            self.error = None
            self._timed_out = False
        thread = threading.Thread(target=self._run, args=(work,), name=f"aipm-{self.name}", daemon=True)
        thread.start()
        if timeout_seconds is not None:
            timer = threading.Timer(timeout_seconds, self._mark_timeout)
            timer.daemon = True
            timer.start()
        return True

    def _run(self, work: Callable[[], Any]) -> None:
        try:
            result = work()
            status = "healthy" if not getattr(result, "error", None) else "unavailable"
        except Exception as exc:
            status = "unavailable"
            self.error = exc
            if self.logger is not None:
                self.logger.exception("Telemetry slow task failed", exc_info=exc)
        else:
            with self._lock:
                if not self._timed_out:
                    self.last_status = status
        finally:
            with self._lock:
                self.last_finished_at = time.monotonic()
                self.last_duration_ms = max(0, int((self.last_finished_at - (self.last_started_at or self.last_finished_at)) * 1000))
                self.running = False

    def _mark_timeout(self) -> None:
        with self._lock:
            if self.running:
                self._timed_out = True
                self.last_status = "timeout"

    def state(self) -> SlowTaskState:
        with self._lock:
            return SlowTaskState(name=self.name, running=self.running, last_started_at=None, last_finished_at=None, last_duration_ms=self.last_duration_ms, last_status=self.last_status, skipped_count=self.skipped_count)


class TelemetrySamplingCoordinator:
    """Run fast telemetry synchronously while slow refreshes execute single-flight in daemon threads."""

    def __init__(self, sampler: TelemetrySampler, config: TelemetryConfig, *, sleeper: Callable[[float], None] | None = None, monotonic: Callable[[], float] | None = None, logger: Any | None = None) -> None:
        self.sampler = sampler
        self.config = config
        self._sleeper = sleeper
        self._monotonic = monotonic or time.monotonic
        self.logger = logger
        self._stop_requested = False
        self._stop_event = threading.Event()
        self.resource_slot = _SingleFlightSlot("resource", logger)
        self.project_slot = _SingleFlightSlot("project", logger)

    def run(self) -> None:
        self._install_signal_handlers()
        now = self._monotonic()
        next_fast = now
        next_resource = now
        next_project = now
        while not self._stop_requested:
            now = self._monotonic()
            if now >= next_resource and self.config.resource_sampling_enabled:
                self.resource_slot.start(self.sampler.refresh_resource_once, timeout_seconds=self.config.resource_timeout_seconds)
                next_resource = now + self.config.resource_interval_seconds
            if now >= next_project:
                self.project_slot.start(self.sampler.refresh_project_once, timeout_seconds=self.config.project_timeout_seconds)
                next_project = now + self.config.project_interval_seconds
            if now >= next_fast:
                result = self.sampler.sample_fast_once()
                if result.error and self.logger is not None:
                    self.logger.error(result.error)
                next_fast += self.config.interval_seconds
                if next_fast <= now:
                    next_fast = now + self.config.interval_seconds
            if self._stop_requested:
                break
            deadline = min(next_fast, next_resource if self.config.resource_sampling_enabled else next_fast, next_project)
            self._sleep_interruptibly(max(0.0, deadline - self._monotonic()))

    def request_stop(self, signum: int | None = None, frame: Any | None = None) -> None:
        self._stop_requested = True
        self._stop_event.set()
        if self.logger is not None:
            self.logger.info("Telemetry sampling coordinator stop requested")

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

    def _sleep_interruptibly(self, seconds: float) -> None:
        if self._sleeper is not None:
            try:
                self._sleeper(seconds)
            except InterruptedError:
                self.request_stop()
            return
        self._stop_event.wait(seconds)
