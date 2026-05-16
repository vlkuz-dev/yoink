from __future__ import annotations

from pathlib import Path


class ProviderError(RuntimeError):
    """Permanent provider failure — pipeline logs and skips, never retries."""

    def __init__(self, message: str, *, url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


class ProviderTransientError(ProviderError):
    """Transient provider failure — eligible for pipeline retry."""


class MediaTooLarge(ProviderError):  # noqa: N818  # name locked in plan
    def __init__(
        self,
        message: str,
        *,
        path: Path | None = None,
        size_bytes: int | None = None,
        limit_bytes: int | None = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes

    @classmethod
    def from_size(
        cls,
        path: Path,
        size_bytes: int,
        limit_bytes: int,
    ) -> MediaTooLarge:
        return cls(
            f"media file {path.name} exceeds limit: {size_bytes}B > {limit_bytes}B",
            path=path,
            size_bytes=size_bytes,
            limit_bytes=limit_bytes,
        )
