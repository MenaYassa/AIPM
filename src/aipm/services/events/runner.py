from __future__ import annotations

import signal
import threading
from typing import Any

from aipm.models.config import EventConfig
from aipm.repositories.events.base import EventRepository
from aipm.repositories.telemetry.base import HistoryRepository
from aipm.services.events.processor import EventProcessor


class EventRunner:
    """Poll committed telemetry runs and process each source run once."""

    def __init__(self, processor: EventProcessor, history_repository: HistoryRepository, event_repository: EventRepository, config: EventConfig, *, logger: Any | None = None) -> None:
        self.processor = processor
        self.history_repository = history_repository
        self.event_repository = event_repository
        self.config = config
        self.logger = logger
        self._stop = threading.Event()
        self._stop_requested = False

    def run(self) -> None:
        self._install_signal_handlers()
        after_id: int | None = None
        while not self._stop_requested:
            runs = self.history_repository.get_runs(after_id, 100)
            if runs:
                for run in runs:
                    if self._stop_requested:
                        break
                    result = self.processor.process_run(run.id)
                    if result.error:
                        if self.logger is not None:
                            self.logger.error(result.error)
                        break
                    after_id = run.id
                continue
            self._stop.wait(self.config.interval_seconds)

    def request_stop(self, signum: int | None = None, frame: Any | None = None) -> None:
        self._stop_requested = True
        self._stop.set()
        if self.logger is not None:
            self.logger.info("Event runner stop requested")

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
