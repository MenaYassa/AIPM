from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class PortInfo:
    host_ip: str
    host_port: int
    container_port: int
    protocol: str