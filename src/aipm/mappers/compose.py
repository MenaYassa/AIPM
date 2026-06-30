# src/aipm/mappers/compose.py
from aipm.models.project import ComposeService

class ComposeMapper:
    @staticmethod
    def map_service(c) -> ComposeService:
        # 1. Safely extract ports
        ports = []
        if getattr(c, 'network_settings', None) and c.network_settings.ports:
            for port, bindings in c.network_settings.ports.items():
                if bindings:
                    for b in bindings:
                        ports.append(f"{b['HostPort']}->{port}")

        # 2. Extract the string status from the ContainerState object
        # We use getattr as a safety net in case the API changes
        state_str = getattr(c.state, 'status', str(c.state))
        
        # 3. Extract the image name safely
        image_name = c.config.image if getattr(c, 'config', None) else "unknown"

        return ComposeService(
            name=c.name,
            image=image_name,
            state=state_str,
            replicas=1,
            ports=ports
        )