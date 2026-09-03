class AIPMError(Exception):
    pass


class ProviderError(AIPMError):
    pass


class DockerError(ProviderError):
    pass


class ContainerNotFound(DockerError):
    pass

class UpdateError(ProviderError):
    """Raised when an automated update transaction fails or must abort."""
    pass


class GitTransactionError(UpdateError):
    """Raised when the update's Git transaction fails; carries the typed GitTransactionResult.

    The result preserves the operator evidence (stashed, pulled, stash_applied,
    stash_preserved, conflicts, warnings, errors) even when the transaction failed,
    so failure classification never depends on exception text alone.
    """

    def __init__(self, message: str, result):
        super().__init__(message)
        self.result = result
