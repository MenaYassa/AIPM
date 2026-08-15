from __future__ import annotations

import logging

from aipm.core.config import ConfigManager
from aipm.core.logger import setup_logger
from aipm.services.docker.service import DockerService
from aipm.services.system.service import SystemService


class Application:
    _instance: Application | None = None

    def __init__(self):
        self.config_manager = ConfigManager()
        self.config = self.config_manager.config
        self.logger = setup_logger(self.config.logging)
        self.logger.debug("AIPM core application initializing")
        self._docker: DockerService | None = None
        self._system: SystemService | None = None

    @property
    def docker(self) -> DockerService:
        if self._docker is None:
            self._docker = DockerService()
            self.logger.debug("DockerService initialized")
        return self._docker

    @property
    def system(self) -> SystemService:
        if self._system is None:
            self._system = SystemService()
            self.logger.debug("SystemService initialized")
        return self._system

    @classmethod
    def create(cls) -> Application:
        """Return the process-wide application context."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton for tests and embedded use."""
        cls._instance = None
