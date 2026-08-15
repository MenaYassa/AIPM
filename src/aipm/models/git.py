# src/aipm/models/git.py
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass(slots=True, frozen=True)
class GitRepository:
    # Core existence
    exists: bool = False

    # Branch & commits
    branch: Optional[str] = None
    current_sha: Optional[str] = None
    remote_sha: Optional[str] = None
    remote_url: Optional[str] = None

    # State flags
    dirty: bool = False
    detached: bool = False

    # Ahead/behind counts
    ahead: int = 0
    behind: int = 0

    # File lists (use factory to avoid shared mutable defaults)
    modified_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    conflicted_files: list[str] = field(default_factory=list)
    stashes: list[str] = field(default_factory=list)

    # Timestamps and metadata
    last_fetch: Optional[datetime] = None
    last_commit_message: Optional[str] = None
    last_commit_author: Optional[str] = None