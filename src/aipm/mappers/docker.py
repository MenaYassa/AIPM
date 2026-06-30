# src/aipm/mappers/docker.py

from datetime import datetime
from aipm.models.container import Container

class DockerMapper:
    @staticmethod
    def container(c) -> Container:
        attrs = c.attrs
        # Using a helper makes the mapping clean and testable
        return Container(
            id=c.short_id,
            name=c.name,
            image=c.image.tags[0] if c.image.tags else "<none>",
            state=attrs.get("State", {}).get("Status", "unknown"),
            health=attrs.get("State", {}).get("Health", {}).get("Status"),
            ports=list(c.ports.keys()),
            labels=c.labels,
            stack=c.labels.get("com.docker.compose.project"),
            created=datetime.fromisoformat(attrs.get("Created", "").replace("Z", "+00:00"))
        )
    @staticmethod
    def inspect_view(c) -> dict:
        attrs = c.attrs
        # Extract IP from network settings
        net_settings = attrs.get("NetworkSettings", {})
        networks = net_settings.get("Networks", {})
        # Get the IP of the first network found
        first_net = next(iter(networks.values()), {})
        ip_addr = first_net.get("IPAddress", "N/A")

        return {
            "name": c.name,
            "image": c.image.tags[0] if c.image.tags else "<none>",
            "state": attrs.get("State", {}).get("Status"),
            "health": attrs.get("State", {}).get("Health", {}).get("Status") or "N/A",
            "ports": [f"{k} -> {v[0]['HostPort']}" for k, v in c.ports.items()] if c.ports else ["None"],
            "networks": list(networks.keys()),
            "ip_address": ip_addr, # New
            "created": attrs.get("Created", "N/A").split("T")[0], # New
            "command": " ".join(attrs.get("Config", {}).get("Cmd", [])), # New
            "mounts": [m.get("Name") or m.get("Source") for m in attrs.get("Mounts", [])],
            "restart_policy": attrs.get("HostConfig", {}).get("RestartPolicy", {}).get("Name")
        }
        
     # Add these to DockerMapper in src/aipm/mappers/docker.py

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if not size_bytes: return "0 B"
        mb = size_bytes / (1024 * 1024)
        if mb > 1024:
            return f"{mb / 1024:.2f} GB"
        return f"{mb:.2f} MB"

    @staticmethod
    def image_view(i) -> dict:
        return {
            "id": i.short_id.replace("sha256:", "")[:12],
            "tags": "\n".join(i.tags) if i.tags else "<none>",
            "size": DockerMapper._format_size(i.attrs.get("Size", 0)),
            "created": i.attrs.get("Created", "N/A").split("T")[0]
        }

    @staticmethod
    def volume_view(v) -> dict:
        return {
            "name": v.name[:40] + "..." if len(v.name) > 40 else v.name,
            "driver": v.attrs.get("Driver", "N/A"),
            "mountpoint": v.attrs.get("Mountpoint", "N/A")
        }

    @staticmethod
    def network_view(n) -> dict:
        return {
            "name": n.name,
            "driver": n.attrs.get("Driver", "N/A"),
            "scope": n.attrs.get("Scope", "N/A")
        }