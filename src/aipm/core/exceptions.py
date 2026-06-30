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