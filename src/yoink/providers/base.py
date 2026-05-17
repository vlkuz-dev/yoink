from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from yoink.core.models import MediaPackage


@runtime_checkable
class Provider(Protocol):
    name: str
    domains: frozenset[str]

    def can_handle(self, url: str) -> bool: ...

    async def fetch(self, url: str, workdir: Path) -> MediaPackage: ...
