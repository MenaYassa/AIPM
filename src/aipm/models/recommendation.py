from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Recommendation:

    priority: int

    action: str

    command: str | None = None