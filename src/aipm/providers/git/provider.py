from __future__ import annotations

from datetime import datetime
import os
import selectors
import signal
import subprocess
import time
from threading import Event

import git
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

from aipm.core.exceptions import ProviderError
from aipm.models.git import GitRepository
from aipm.models.project import Project


class GitError(ProviderError):
    """Raised when a Git operation cannot be completed safely."""


class GitDiscoveryCancelled(GitError):
    """Raised when telemetry Git enrichment is cooperatively cancelled."""


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

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            process.terminate()
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()
            process.wait(timeout=0.25)

    @classmethod
    def _run_bounded_git(cls, path: str, args: tuple[str, ...], *, timeout_seconds: float, output_limit: int, cancel_event: Event | None = None, deadline: float | None = None) -> str:
        if timeout_seconds <= 0 or output_limit <= 0:
            raise GitError("invalid Git bounds")
        environment = os.environ.copy()
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        process = subprocess.Popen(
            # Cross-ownership repositories (e.g. daemon runs as `aipm`, repos owned by
            # `mina`) would otherwise fail with "dubious ownership"; telemetry git
            # commands are bounded read-only snapshots, so trust only the exact path.
            ("git", "-c", f"safe.directory={path}", *args),
            cwd=path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            env=environment,
        )
        selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        buffers: dict[int, bytearray] = {process.stdout.fileno(): bytearray(), process.stderr.fileno(): bytearray()}
        end = min(time.monotonic() + timeout_seconds, deadline) if deadline is not None else time.monotonic() + timeout_seconds
        try:
            while selector.get_map() or process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise GitDiscoveryCancelled("Git telemetry cancelled")
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise GitError("Git command timeout")
                for key, _ in selector.select(min(remaining, 0.05)):
                    chunk = os.read(key.fd, min(8192, output_limit - len(buffers[key.fd]) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[key.fd].extend(chunk)
                    if len(buffers[key.fd]) > output_limit:
                        raise GitError("Git output bound reached")
            returncode = process.wait(timeout=max(0.0, end - time.monotonic()))
            stdout = bytes(buffers[process.stdout.fileno()])
            stderr = bytes(buffers[process.stderr.fileno()])
            if returncode != 0:
                raise GitError("bounded Git command failed")
            return stdout.decode("utf-8", errors="replace")
        except (GitError, GitDiscoveryCancelled):
            cls._terminate_process(process)
            raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            cls._terminate_process(process)
            raise GitError("bounded Git command failed") from exc
        finally:
            selector.close()

    def repository_bounded(self, project: Project, *, timeout_seconds: float, max_items: int, cancel_event: Event | None = None, deadline: float | None = None) -> GitRepository:
        """Return a bounded read-only Git snapshot for telemetry."""
        if max_items <= 0:
            raise GitError("invalid Git item bound")
        output_limit = max(4096, max_items * 256)
        path = project.path
        self._run_bounded_git(path, ("rev-parse", "--is-inside-work-tree"), timeout_seconds=timeout_seconds, output_limit=output_limit, cancel_event=cancel_event, deadline=deadline)
        try:
            current_sha = self._run_bounded_git(path, ("rev-parse", "HEAD"), timeout_seconds=timeout_seconds, output_limit=output_limit, cancel_event=cancel_event, deadline=deadline).strip() or None
        except GitError:
            # Unborn branch or unreadable ref: report what git can still tell us.
            current_sha = None
        try:
            branch_text = self._run_bounded_git(path, ("symbolic-ref", "--quiet", "--short", "HEAD"), timeout_seconds=timeout_seconds, output_limit=output_limit, cancel_event=cancel_event, deadline=deadline)
        except GitError:
            branch_text = ""
        branch = branch_text.strip() or None
        status = self._run_bounded_git(path, ("status", "--porcelain=v1", "--untracked-files=all"), timeout_seconds=timeout_seconds, output_limit=output_limit, cancel_event=cancel_event, deadline=deadline)
        modified: list[str] = []
        untracked: list[str] = []
        conflicted: list[str] = []
        for line in status.splitlines():
            if len(modified) + len(untracked) + len(conflicted) >= max_items:
                break
            if len(line) < 4:
                continue
            code, name = line[:2], line[3:]
            if code == "??":
                untracked.append(name)
            else:
                modified.append(name)
            if "U" in code or code in {"AA", "DD"}:
                conflicted.append(name)
        try:
            stash = self._run_bounded_git(path, ("stash", "list", "--format=%gd %s"), timeout_seconds=timeout_seconds, output_limit=output_limit, cancel_event=cancel_event, deadline=deadline)
            stashes = stash.splitlines()[:max_items]
        except GitError:
            stashes = []
        ahead = behind = 0
        if branch:
            try:
                counts = self._run_bounded_git(path, ("rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"), timeout_seconds=timeout_seconds, output_limit=output_limit, cancel_event=cancel_event, deadline=deadline).split()
            except GitError:
                counts = []
            if len(counts) == 2 and all(item.isdigit() for item in counts):
                behind, ahead = (int(counts[0]), int(counts[1]))
        try:
            metadata = self._run_bounded_git(path, ("log", "-1", "--format=%H%x00%s%x00%an"), timeout_seconds=timeout_seconds, output_limit=output_limit, cancel_event=cancel_event, deadline=deadline).strip("\n").split("\x00")
        except GitError:
            metadata = []
        return GitRepository(
            exists=True,
            branch=branch,
            current_sha=current_sha,
            remote_sha=None,
            remote_url=None,
            dirty=bool(modified or untracked),
            detached=branch is None,
            ahead=min(ahead, max_items),
            behind=min(behind, max_items),
            modified_files=modified[:max_items],
            untracked_files=untracked[:max_items],
            conflicted_files=conflicted[:max_items],
            stashes=stashes,
            last_fetch=None,
            last_commit_message=metadata[1] if len(metadata) > 1 else None,
            last_commit_author=metadata[2] if len(metadata) > 2 else None,
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
