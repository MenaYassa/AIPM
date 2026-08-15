from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class GitUpdatePlan:

    proceed: bool

    stash_required: bool

    fetch_required: bool

    pull_required: bool

    review_required: bool

    rollback_required: bool

    reasons: list[str] = field(default_factory=list)
