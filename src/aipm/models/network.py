from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class NetworkInfo:
    name: str
    driver: str
    scope: str