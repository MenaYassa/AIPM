import git
from git.exc import InvalidGitRepositoryError, GitCommandError
from aipm.core.exceptions import ProviderError
from aipm.models.project import Project

class GitError(ProviderError):
    pass

class GitProvider:
    def enrich(self, project: Project):
        """Populates the git state directly onto the Project model."""
        if not project.capabilities.has_git:
            return
            
        try:
            repo = git.Repo(project.path)
            project.git_branch = repo.active_branch.name
            # True if there are modified or untracked files
            project.git_dirty = repo.is_dirty(untracked_files=True)
        except TypeError:
            # Handle detached HEAD states safely
            project.git_branch = "detached"
            project.git_dirty = repo.is_dirty(untracked_files=True)
        except Exception as e:
            raise GitError(f"Failed to read git state for {project.name}: {e}")

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