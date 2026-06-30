# src/aipm/core/config.py
import yaml
from pathlib import Path
from aipm.models.config import AIPMConfig, LoggingConfig, DiscoveryConfig
from aipm.core.exceptions import AIPMError

class ConfigManager:
    def __init__(self, config_path: str = "config/aipm.yaml"):
        self.config_path = Path(config_path)
        self.config: AIPMConfig = self._load_or_create()

    def _load_or_create(self) -> AIPMConfig:
        if not self.config_path.exists():
            return self._create_default()
        
        try:
            with open(self.config_path, "r") as f:
                data = yaml.safe_load(f) or {}
            
            # Map dictionary to our domain models
            log_data = data.get("logging", {})
            disc_data = data.get("discovery", {})
            
            return AIPMConfig(
                logging=LoggingConfig(**log_data),
                discovery=DiscoveryConfig(**disc_data)
            )
        except Exception as e:
            raise AIPMError(f"Failed to load configuration: {e}")

    def _create_default(self) -> AIPMConfig:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        default_config = AIPMConfig()
        
        # Save the default representation to disk
        config_dict = {
            "logging": default_config.logging.__dict__,
            "discovery": default_config.discovery.__dict__
        }
        
        with open(self.config_path, "w") as f:
            yaml.dump(config_dict, f, default_flow_style=False)
            
        return default_config