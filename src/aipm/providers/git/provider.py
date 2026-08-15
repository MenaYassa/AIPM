from __future__ import annotations

from datetime import datetime

import git
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

from aipm.core.exceptions import ProviderError
from aipm.models.git import GitRepository
from aipm.models.project import Project


class GitError(ProviderError):
    """Raised when a Git operation cannot be completed safely."""


class GitProvider:
    @staticmethod
    def _empty_repository() -> GitRepository:
        return GitRepository()

    def _repo(self, project: Project):
        try:
            return git.Repo(project.path)
        except (InvalidGitRepositoryError, NoSuchPathError, GitCommandError) as exc:
            raise GitError(f"'{project.name}' is not a valid Git repository: {exc}") from exc

    @staticmethod
    def _origin(repo):
        try:
            return repo.remotes.origin
        except (AttributeError, IndexError):
            return None

    @staticmethod
    def _current_commit(repo):
        try:
            return repo.head.commit
        except (ValueError, TypeError, GitCommandError):
            return None

    def _remote_commit(self, repo, branch: str | None):
        if not branch:
            return None
        try:
            return repo.refs[f"origin/{branch}"].commit
        except (IndexError, AttributeError, ValueError, TypeError):
            return None

    def _ahead_behind(self, repo, branch: str | None, remote_commit) -> tuple[int, int]:
        if not branch or remote_commit is None:
            return 0, 0
        try:
            return (
                sum(1 for _ in repo.iter_commits(f"origin/{branch}..HEAD")),
                sum(1 for _ in repo.iter_commits(f"HEAD..origin/{branch}")),
            )
        except (GitCommandError, ValueError, TypeError):
            return 0, 0

    @staticmethod
    def _stash_list(repo) -> list[str]:
        try:
            output = repo.git.stash("list")
        except GitCommandError:
            return []
        return output.splitlines() if output else []

    @staticmethod
    def _changed_files(repo) -> list[str]:
        changed: set[str] = set()
        try:
            changed.update(item.a_path for item in repo.index.diff(None) if item.a_path)
            changed.update(item.a_path for item in repo.index.diff("HEAD") if item.a_path)
        except (GitCommandError, ValueError, TypeError):
            pass
        return sorted(changed)

    def repository(self, project: Project) -> GitRepository:
        """Return a read-only repository snapshot without contacting a remote."""
        try:
            repo = git.Repo(project.path)
        except (InvalidGitRepositoryError, NoSuchPathError, GitCommandError):
            return self._empty_repository()

        current = self._current_commit(repo)
        detached = bool(repo.head.is_detached)
        try:
            branch = None if detached else repo.active_branch.name
        except (TypeError, ValueError, GitCommandError):
            branch = None

        remote = self._remote_commit(repo, branch)
        ahead, behind = self._ahead_behind(repo, branch, remote)
        origin = self._origin(repo)
        remote_url = next(iter(origin.urls), None) if origin is not None else None
        untracked_files = sorted(repo.untracked_files)

        return GitRepository(
            exists=True,
            branch=branch,
            current_sha=current.hexsha if current else None,
            remote_sha=remote.hexsha if remote else None,
            remote_url=remote_url,
            dirty=repo.is_dirty(untracked_files=True),
            detached=detached,
            ahead=ahead,
            behind=behind,
            modified_files=self._changed_files(repo),
            untracked_files=untracked_files,
            conflicted_files=sorted(repo.index.unmerged_blobs().keys()),
            stashes=self._stash_list(repo),
            last_fetch=None,
            last_commit_message=current.message.strip() if current else None,
            last_commit_author=current.author.name if current else None,
        )

    def fetch(self, project: Project) -> None:
        repo = self._repo(project)
        origin = self._origin(repo)
        if origin is None:
            raise GitError(f"Project '{project.name}' has no remote named 'origin'.")
        try:
            origin.fetch()
        except GitCommandError as exc:
            raise GitError(f"Git fetch failed: {exc}") from exc

    def pull(self, project: Project) -> None:
        if not project.capabilities.has_git:
            raise GitError(f"Project '{project.name}' is not a Git repository.")
        repo = self._repo(project)
        if repo.is_dirty(untracked_files=True):
            raise GitError("Cannot pull: project has uncommitted local changes.")
        origin = self._origin(repo)
        if origin is None:
            raise GitError("No remote 'origin' found to pull from.")
        try:
            origin.pull()
        except GitCommandError as exc:
            detail = exc.stderr.strip() if exc.stderr else str(exc)
            raise GitError(f"Git pull failed: {detail}") from exc

    def stash(self, project: Project, message: str = "AIPM safety stash") -> None:
        try:
            self._repo(project).git.stash("push", "-u", "-m", message)
        except GitCommandError as exc:
            raise GitError(f"Git stash failed: {exc}") from exc

    def apply_stash(self, project: Project) -> None:
        try:
            self._repo(project).git.stash("apply")
        except GitCommandError as exc:
            raise GitError(f"Applying the Git stash failed: {exc}") from exc

    def drop_stash(self, project: Project) -> None:
        try:
            self._repo(project).git.stash("drop")
        except GitCommandError as exc:
            raise GitError(f"Dropping the Git stash failed: {exc}") from exc

    def abort_merge(self, project: Project) -> None:
        try:
            self._repo(project).git.merge("--abort")
        except GitCommandError as exc:
            raise GitError(f"Aborting the merge failed: {exc}") from exc

    def abort_rebase(self, project: Project) -> None:
        try:
            self._repo(project).git.rebase("--abort")
        except GitCommandError as exc:
            raise GitError(f"Aborting the rebase failed: {exc}") from exc

    def restore(self, project: Project) -> None:
        try:
            self._repo(project).git.restore(".")
        except GitCommandError as exc:
            raise GitError(f"Restoring the worktree failed: {exc}") from exc

    def current_sha(self, project: Project) -> str:
        current = self._current_commit(self._repo(project))
        if current is None:
            raise GitError(f"Project '{project.name}' has no commits yet.")
        return current.hexsha

    def remote_sha(self, project: Project) -> str | None:
        repo = self._repo(project)
        try:
            branch = None if repo.head.is_detached else repo.active_branch.name
        except (TypeError, ValueError, GitCommandError):
            branch = None
        remote = self._remote_commit(repo, branch)
        return remote.hexsha if remote else None

    def diff(self, project: Project) -> str:
        try:
            return self._repo(project).git.diff()
        except GitCommandError as exc:
            raise GitError(f"Unable to read Git diff: {exc}") from exc

    def changed_files(self, project: Project) -> list[str]:
        return self._changed_files(self._repo(project))
