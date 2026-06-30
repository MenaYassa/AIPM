# /home/ubuntu/aipm/src/aipm/core/app.py

from aipm.core.config import ConfigManager
from aipm.core.logger import setup_logger
from aipm.services.docker.service import DockerService
from aipm.services.system.service import SystemService  # <-- Add this import
import logging

class Application:
    _instance = None

    def __init__(self):
        # 1. Boot the configuration system
        self.config_manager = ConfigManager()
        self.config = self.config_manager.config
        
        # 2. Boot the logging system
        self.logger = setup_logger(self.config.logging)
        self.logger.debug("AIPM Core Application initializing...")
        
        # 3. Instantiate domain services
        self.docker = DockerService()
        self.logger.debug("DockerService initialized.")
        
        # 4. Attach the system telemetry engine
        self.system = SystemService()  # <-- Add this instantiation
        self.logger.debug("SystemService initialized.")

    @classmethod
    def create(cls):
        """Singleton pattern ensures config and logger are only loaded once per command."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance