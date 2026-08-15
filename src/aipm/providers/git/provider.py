import git
from git.exc import InvalidGitRepositoryError, GitCommandError
from aipm.core.exceptions import ProviderError
from aipm.models.project import Project
from aipm.models.git import GitRepository


class GitError(ProviderError):
    pass


class GitProvider:

    # ---------- Helper methods for repository snapshot ----------
    def _current_commit(self, repo):
        return repo.head.commit

    def _remote_commit(self, repo):
        try:
            repo.remotes.origin.fetch()
            branch = repo.active_branch.name
            return repo.refs[f"origin/{branch}"].commit
        except Exception:
            return None

    def _ahead_behind(self, repo):
        try:
            branch = repo.active_branch.name
            origin = repo.refs[f"origin/{branch}"]
            ahead = sum(1 for _ in repo.iter_commits(f"origin/{branch}..HEAD"))
            behind = sum(1 for _ in repo.iter_commits(f"HEAD..origin/{branch}"))
            return ahead, behind
        except Exception:
            return 0, 0

    def _stash_list(self, repo):
        output = repo.git.stash("list")
        if not output:
            return []
        return output.splitlines()

    def repository(self, project: Project) -> GitRepository:
        """Returns a complete GitRepository snapshot for the project."""
        try:
            repo = git.Repo(project.path)

            current = self._current_commit(repo)
            remote = self._remote_commit(repo)
            ahead, behind = self._ahead_behind(repo)

            # Remote URL
            try:
                remote_url = next(repo.remote().urls)
            except (AttributeError, StopIteration):
                remote_url = None

            return GitRepository(
                exists=True,
                branch=repo.active_branch.name,
                current_sha=current.hexsha,
                remote_sha=remote.hexsha if remote else None,
                remote_url=remote_url,
                dirty=repo.is_dirty(untracked_files=True),
                detached=repo.head.is_detached,
                ahead=ahead,
                behind=behind,
                modified_files=self.changed_files(project),
                untracked_files=repo.untracked_files,
                conflicted_files=list(repo.index.unmerged_blobs().keys()),
                stashes=self._stash_list(repo),
                last_fetch=None,
                last_commit_message=current.message.strip(),
                last_commit_author=current.author.name,
            )
        except (InvalidGitRepositoryError, TypeError, AttributeError, GitCommandError, Exception):
            # Not a git repo or any other error -> return an empty/unavailable model
            return GitRepository(
                exists=False,
                branch=None,
                current_sha=None,
                remote_sha=None,
                remote_url=None,
                dirty=False,
                detached=False,
                ahead=0,
                behind=0,
                modified_files=[],
                untracked_files=[],
                conflicted_files=[],
                stashes=[],
                last_fetch=None,
                last_commit_message=None,
                last_commit_author=None,
            )

    # ---------- Git action methods ----------
    def fetch(self, project: Project):
        repo = git.Repo(project.path)
        repo.remotes.origin.fetch()

    def pull(self, project: Project):
        """Pulls the latest changes from the remote tracking branch."""
        if not project.capabilities.has_git:
            raise GitError(f"Project '{project.name}' is not a git repository.")

        try:
            repo = git.Repo(project.path)
            if repo.is_dirty(untracked_files=True):
                raise GitError("Cannot pull: Project has uncommitted local changes.")

            origin = repo.remotes.origin
            origin.pull()
        except GitCommandError as e:
            raise GitError(f"Git pull failed: {e.stderr}")
        except AttributeError:
            raise GitError("No remote 'origin' found to pull from.")

    def stash(self, project: Project, message: str):
        repo = git.Repo(project.path)
        repo.git.stash("push", "-u", "-m", message)

    def apply_stash(self, project: Project):
        repo = git.Repo(project.path)
        repo.git.stash("apply")

    def drop_stash(self, project: Project):
        repo = git.Repo(project.path)
        repo.git.stash("drop")

    def abort_merge(self, project: Project):
        repo = git.Repo(project.path)
        repo.git.merge("--abort")

    def abort_rebase(self, project: Project):
        repo = git.Repo(project.path)
        repo.git.rebase("--abort")

    def restore(self, project: Project):
        repo = git.Repo(project.path)
        repo.git.restore(".")

    # ---------- Additional read-only methods (used internally) ----------
    def current_sha(self, project: Project) -> str:
        repo = git.Repo(project.path)
        return repo.head.commit.hexsha

    def remote_sha(self, project: Project) -> str | None:
        repo = git.Repo(project.path)
        try:
            repo.remotes.origin.fetch()
            branch = repo.active_branch.name
            return repo.refs[f"origin/{branch}"].commit.hexsha
        except Exception:
            return None

    def diff(self, project: Project) -> str:
        repo = git.Repo(project.path)
        return repo.git.diff()

    def changed_files(self, project: Project) -> list[str]:
        repo = git.Repo(project.path)
        return [item.a_path for item in repo.index.diff(None)]