from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class RestoreResult:
    """Outcome of a restore-point operation against a project directory."""

    attempted: bool
    success: bool
    restored: list[str] = field(default_factory=list)
    left_in_place: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str | None = None