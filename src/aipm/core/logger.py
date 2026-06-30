# src/aipm/core/logger.py

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from aipm.models.config import LoggingConfig

def setup_logger(config: LoggingConfig) -> logging.Logger:
    logger = logging.getLogger("aipm")
    
    # Prevent adding handlers multiple times if instantiated twice
    if logger.handlers:
        return logger

    # Set base level
    logger.setLevel(getattr(logging, config.level.upper(), logging.INFO))
    
    # Ensure log directory exists
    log_file = Path(config.file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    # Setup rotating file handler (e.g., 10MB per file, keep 3 backups)
    max_bytes = config.max_size_mb * 1024 * 1024
    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=config.backup_count
    )
    
    # Standard production log format
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - [%(module)s:%(funcName)s] - %(message)s"
    )
    file_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    
    # We purposefully DO NOT add a StreamHandler here. 
    # The console output is managed exclusively by our Capability layer using Rich.
    
    return logger