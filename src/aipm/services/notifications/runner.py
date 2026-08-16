from __future__ import annotations

import signal
import threading
from typing import Any

from aipm.models.config import NotificationConfig
from aipm.services.notifications.worker import NotificationProjector, NotificationWorker


class NotificationRunner:
    def __init__(self, projector: NotificationProjector, worker: NotificationWorker, config: NotificationConfig, *, logger: Any | None = None):
        self.projector = projector
        self.worker = worker
        self.config = config
        self.logger = logger
        self._stop = threading.Event()
        self._stop_requested = False

    def run(self) -> None:
        self._install_signal_handlers()
        while not self._stop_requested:
            projected = self.projector.project_once()
            delivered = self.worker.deliver_once()
            if not projected and not delivered:
                self._stop.wait(self.config.interval_seconds)

    def request_stop(self, signum: int | None = None, frame: Any | None = None) -> None:
        self._stop_requested = True
        self._stop.set()
        if self.logger is not None:
            self.logger.info("Notification runner stop requested")

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
