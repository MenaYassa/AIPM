from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class GitTransactionResult:

    success: bool

    stashed: bool

    pulled: bool

    stash_applied: bool

    stash_preserved: bool = False

    conflicts: list[str] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)

    errors: list[str] = field(default_factory=list)
