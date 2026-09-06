from __future__ import annotations

from aipm.core.exceptions import GitTransactionError
from aipm.models.git_transaction import GitTransactionResult
from aipm.services.git.service import GitService


class GitTransactionRunner:
    """Run the update's Git phases as one classified, typed transaction.

    Wraps the existing provider operations (stash / fetch / pull / apply / drop)
    without adding new Git commands. Contract:

    - The stash is dropped only after a successful apply; on any apply failure
      the stash is preserved and the exact conflicting files are reported.
    - Only a stash this transaction actually created is ever applied or dropped.
      ``git stash push`` exits zero and creates nothing when the tree is clean,
      so the entry count is compared before and after: unless a new entry
      appeared, no apply and no drop run and pre-existing stashes owned by the
      operator are left untouched.
    - Operator changes are never discarded: no checkout/reset/clean runs, and a
      failed apply leaves the merge state for manual resolution.
    - Every failure raises ``GitTransactionError`` carrying the typed
      ``GitTransactionResult`` (stashed/pulled/stash_applied/stash_preserved,
      conflicts, warnings, errors) for the audit record.
    """

    def __init__(self, git_service: GitService | None = None):
        self.git_service = git_service or GitService()

    def _stash_count(self, project) -> int:
        return len(self.git_service.repository(project).stashes)

    def run(
        self,
        project,
        *,
        stash_required: bool,
        fetch_required: bool,
        pull_required: bool,
    ) -> GitTransactionResult:
        stashed = False
        pulled = False
        stash_applied = False
        stash_preserved = False
        conflicts: list[str] = []
        warnings: list[str] = []
        errors: list[str] = []

        if stash_required:
            try:
                stashes_before = self._stash_count(project)
            except Exception as exc:
                result = GitTransactionResult(
                    success=False,
                    stashed=False,
                    pulled=False,
                    stash_applied=False,
                    stash_preserved=False,
                    conflicts=[],
                    warnings=warnings,
                    errors=[f"stash inspection failed: {exc}"],
                )
                raise GitTransactionError(
                    "Git transaction refused to create a safety stash because the existing stash "
                    f"entries could not be read; nothing was changed: {exc}",
                    result,
                ) from exc
            try:
                self.git_service.stash(project, "AIPM safety stash")
            except Exception as exc:
                result = GitTransactionResult(
                    success=False,
                    stashed=False,
                    pulled=False,
                    stash_applied=False,
                    stash_preserved=False,
                    conflicts=[],
                    warnings=warnings,
                    errors=[f"stash failed: {exc}"],
                )
                raise GitTransactionError(f"Git transaction failed while creating the safety stash: {exc}", result) from exc
            try:
                stashed = self._stash_count(project) > stashes_before
            except Exception as exc:
                # The push may have created an entry; refuse to apply or drop
                # anything we cannot identify and leave it for manual recovery.
                result = GitTransactionResult(
                    success=False,
                    stashed=True,
                    pulled=False,
                    stash_applied=False,
                    stash_preserved=True,
                    conflicts=[],
                    warnings=warnings,
                    errors=[f"stash verification failed: {exc}"],
                )
                raise GitTransactionError(
                    "Git transaction could not verify the safety stash it created; any stashed "
                    f"local changes were preserved for manual recovery: {exc}",
                    result,
                ) from exc
            if not stashed:
                warnings.append(
                    "no local changes were stashed; existing stash entries were left untouched"
                )

        try:
            if fetch_required:
                self.git_service.fetch(project)
            if pull_required:
                self.git_service.pull(project)
                pulled = True
        except Exception as exc:
            errors.append(f"fetch/pull failed: {exc}")
            result = GitTransactionResult(
                success=False,
                stashed=stashed,
                pulled=pulled,
                stash_applied=False,
                stash_preserved=stashed,
                conflicts=[],
                warnings=warnings,
                errors=errors,
            )
            message = "Git transaction failed during fetch/pull"
            if stashed:
                message += "; local changes remain preserved in the safety stash"
            raise GitTransactionError(message + f": {exc}", result) from exc

        if stashed:
            try:
                self.git_service.apply_stash(project)
                stash_applied = True
            except Exception as exc:
                stash_preserved = True
                # Read-only enumeration of the exact conflicting files left by the
                # failed apply; never mutates anything to "make the update work".
                try:
                    conflicts = list(self.git_service.conflicted_files(project))
                except Exception:
                    conflicts = []
                errors.append(f"stash apply failed: {exc}")
                result = GitTransactionResult(
                    success=False,
                    stashed=stashed,
                    pulled=pulled,
                    stash_applied=False,
                    stash_preserved=True,
                    conflicts=conflicts,
                    warnings=warnings,
                    errors=errors,
                )
                raise GitTransactionError(
                    "Git transaction failed applying the safety stash; the stash was preserved "
                    f"for manual recovery. Conflicting files: {', '.join(conflicts) if conflicts else 'not enumerable'}. {exc}",
                    result,
                ) from exc
            self.git_service.drop_stash(project)
        return GitTransactionResult(
            success=True,
            stashed=stashed,
            pulled=pulled,
            stash_applied=stash_applied,
            stash_preserved=False,
            conflicts=conflicts,
            warnings=warnings,
            errors=errors,
        )
