from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aipm.models.container import Container


class DockerMapper:
    @staticmethod
    def _created(value: Any) -> datetime:
        if not value:
            return datetime.fromtimestamp(0, tz=timezone.utc)
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return datetime.fromtimestamp(0, tz=timezone.utc)

    @staticmethod
    def _image_name(container: Any) -> str:
        image = getattr(container, "image", None)
        tags = getattr(image, "tags", None) or []
        return tags[0] if tags else "<none>"

    @staticmethod
    def container(container: Any) -> Container:
        attrs = getattr(container, "attrs", {}) or {}
        state = attrs.get("State", {}) or {}
        labels = getattr(container, "labels", None) or attrs.get("Config", {}).get("Labels", {}) or {}
        ports = getattr(container, "ports", None) or {}
        return Container(
            id=getattr(container, "short_id", getattr(container, "id", "unknown")),
            name=getattr(container, "name", "unknown"),
            image=DockerMapper._image_name(container),
            state=state.get("Status", "unknown"),
            health=(state.get("Health") or {}).get("Status"),
            ports=list(ports.keys()),
            labels=labels,
            stack=labels.get("com.docker.compose.project"),
            created=DockerMapper._created(attrs.get("Created")),
        )

    @staticmethod
    def inspect_view(container: Any) -> dict[str, Any]:
        attrs = getattr(container, "attrs", {}) or {}
        state = attrs.get("State", {}) or {}
        network_settings = attrs.get("NetworkSettings", {}) or {}
        networks = network_settings.get("Networks", {}) or {}
        first_network = next(iter(networks.values()), {}) or {}
        ports = getattr(container, "ports", None) or {}
        formatted_ports = []
        for container_port, bindings in ports.items():
            if bindings:
                formatted_ports.extend(
                    f"{binding.get('HostIp', '0.0.0.0')}:{binding.get('HostPort', '?')} -> {container_port}"
                    for binding in bindings
                )
            else:
                formatted_ports.append(str(container_port))

        return {
            "name": getattr(container, "name", "unknown"),
            "image": DockerMapper._image_name(container),
            "state": state.get("Status", "unknown"),
            "health": (state.get("Health") or {}).get("Status") or "N/A",
            "ports": formatted_ports or ["None"],
            "networks": list(networks.keys()),
            "ip_address": first_network.get("IPAddress", "N/A"),
            "created": str(attrs.get("Created", "N/A")).split("T")[0],
            "command": " ".join(attrs.get("Config", {}).get("Cmd", []) or []),
            "mounts": [mount.get("Name") or mount.get("Source") for mount in attrs.get("Mounts", [])],
            "restart_policy": attrs.get("HostConfig", {}).get("RestartPolicy", {}).get("Name"),
        }

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if not size_bytes:
            return "0 B"
        megabytes = size_bytes / (1024 * 1024)
        if megabytes > 1024:
            return f"{megabytes / 1024:.2f} GB"
        return f"{megabytes:.2f} MB"

    @staticmethod
    def image_view(image: Any) -> dict[str, str]:
        short_id = getattr(image, "short_id", "")
        return {
            "id": short_id.replace("sha256:", "")[:12],
            "tags": "\n".join(getattr(image, "tags", None) or []) or "<none>",
            "size": DockerMapper._format_size((getattr(image, "attrs", {}) or {}).get("Size", 0)),
            "created": str((getattr(image, "attrs", {}) or {}).get("Created", "N/A")).split("T")[0],
        }

    @staticmethod
    def volume_view(volume: Any) -> dict[str, str]:
        name = getattr(volume, "name", "unknown")
        attrs = getattr(volume, "attrs", {}) or {}
        return {
            "name": name[:40] + "..." if len(name) > 40 else name,
            "driver": attrs.get("Driver", "N/A"),
            "mountpoint": attrs.get("Mountpoint", "N/A"),
        }

    @staticmethod
    def network_view(network: Any) -> dict[str, str]:
        attrs = getattr(network, "attrs", {}) or {}
        return {
            "name": getattr(network, "name", "unknown"),
            "driver": attrs.get("Driver", "N/A"),
            "scope": attrs.get("Scope", "N/A"),
        }
