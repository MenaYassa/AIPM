from dataclasses import dataclass

@dataclass(slots=True, frozen=True)
class ImageInfo:
    id: str
    repository: str
    tag: str
    digest: str | None
    created: str | None
    size: int | None