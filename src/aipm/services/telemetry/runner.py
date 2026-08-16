from __future__ import annotations

import signal
import threading
from typing import Any, Callable

from aipm.models.config import TelemetryConfig
from aipm.services.telemetry.sampler import TelemetrySampler


class TelemetryRunner:
    """Run one observation-only sampler process at the configured interval."""

    def __init__(
        self,
        sampler: TelemetrySampler,
        config: TelemetryConfig,
        *,
        sleeper: Callable[[float], None] | None = None,
        logger: Any | None = None,
    ) -> None:
        self.sampler = sampler
        self.config = config
        self._sleeper = sleeper
        self.logger = logger
        self._stop_requested = False
        self._stop_event = threading.Event()

    def run(self) -> None:
        self._install_signal_handlers()
        while not self._stop_requested:
            result = self.sampler.sample_once()
            if result.error and self.logger is not None:
                self.logger.error(result.error)
            if self._stop_requested:
                break
            self._sleep_interruptibly(self.config.interval_seconds)

    def request_stop(self, signum: int | None = None, frame: Any | None = None) -> None:
        self._stop_requested = True
        self._stop_event.set()
        if self.logger is not None:
            self.logger.info("Telemetry runner stop requested")

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

    def _sleep_interruptibly(self, seconds: int) -> None:
        if self._sleeper is not None:
            try:
                self._sleeper(seconds)
            except InterruptedError:
                self.request_stop()
            return
        self._stop_event.wait(seconds)
