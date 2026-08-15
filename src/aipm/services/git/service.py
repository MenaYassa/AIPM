from aipm.models.project import Project
from aipm.models.git import GitRepository
from aipm.models.git_transaction import GitTransactionResult
from aipm.models.git_update_plan import GitUpdatePlan
from aipm.providers.git.provider import GitProvider
from aipm.services.git.conflicts import ConflictAnalyzer


class GitService:

    def __init__(self):
        self.provider = GitProvider()
        self.conflicts = ConflictAnalyzer()

    def repository(self, project: Project) -> GitRepository:
        """
        Returns the complete Git domain model for a project.
        """
        return self.provider.repository(project)

    def exists(self, project: Project) -> bool:
        return self.repository(project).exists

    def is_dirty(self, project: Project) -> bool:
        return self.repository(project).dirty

    def current_branch(self, project: Project) -> str | None:
        return self.repository(project).branch

    def ahead(self, project: Project) -> int:
        return self.repository(project).ahead

    def behind(self, project: Project) -> int:
        return self.repository(project).behind

    def modified_files(self, project: Project) -> list[str]:
        return self.repository(project).modified_files

    def untracked_files(self, project: Project) -> list[str]:
        return self.repository(project).untracked_files

    def conflicted_files(self, project: Project) -> list[str]:
        return self.repository(project).conflicted_files

    def fetch(self, project: Project):
        self.provider.fetch(project)

    def pull(self, project: Project):
        self.provider.pull(project)

    def stash(self, project: Project, message: str):
        self.provider.stash(project, message)

    def apply_stash(self, project: Project):
        self.provider.apply_stash(project)

    def drop_stash(self, project: Project):
        self.provider.drop_stash(project)

    def diff(self, project: Project) -> str:
        return self.provider.diff(project)

    def current_sha(self, project: Project) -> str:
        return self.provider.current_sha(project)

    def remote_sha(self, project: Project) -> str | None:
        return self.provider.remote_sha(project)

    def changed_files(self, project: Project) -> list[str]:
        return self.provider.changed_files(project)

    def prepare_update(self, project: Project) -> GitUpdatePlan:
        """
        Analyzes the repository state and returns a plan for updating.
        """
        repo = self.repository(project)
        reasons = []
        stash_required = False
        review_required = False

        # Check if repository exists
        if not repo.exists:
            reasons.append("Project is not a Git repository.")
            return GitUpdatePlan(
                proceed=False,
                stash_required=False,
                fetch_required=False,
                pull_required=False,
                review_required=True,
                rollback_required=False,
                reasons=reasons,
            )

        # Detached HEAD
        if repo.detached:
            reasons.append("Repository is in detached HEAD state.")
            review_required = True

        # Local commits ahead
        if repo.ahead:
            reasons.append("Local commits have not been pushed.")
            review_required = True

        # Behind (available pulls)
        if repo.behind:
            reasons.append(f"{repo.behind} commits available.")

        # Dirty state - classify modified files
        if repo.conflicted_files:
            review_required = True
            reasons.append(f"Unresolved merge conflicts in {len(repo.conflicted_files)} file(s).")
        elif repo.dirty:
            modified = list(dict.fromkeys(repo.modified_files + repo.untracked_files))
            classification = self.conflicts.classify(modified)
            if classification["critical"]:
                review_required = True
                reasons.append(
                    "Critical infrastructure files modified: "
                    + ", ".join(classification["critical"])
                    + "."
                )
            else:
                stash_required = True
                reasons.append("Uncommitted changes detected in non-critical files; AIPM can preserve them in a safety stash.")

        # Existing stashes (warn but don't block)
        if repo.stashes:
            reasons.append(f"{len(repo.stashes)} stash(es) exist.")

        return GitUpdatePlan(
            proceed=not review_required,
            stash_required=stash_required,
            fetch_required=bool(repo.remote_url),
            pull_required=bool(repo.remote_url) and repo.behind > 0,
            review_required=review_required,
            rollback_required=False,
            reasons=reasons,
        )