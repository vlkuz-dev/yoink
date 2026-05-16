from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from yoink.core.models import MediaPackage


@dataclass
class _DummyProvider:
    name: str = "dummy"
    domains: frozenset[str] = field(default_factory=lambda: frozenset({"dummy.test"}))

    def can_handle(self, url: str) -> bool:
        return "dummy.test" in url

    async def fetch(self, url: str, workdir: Path) -> MediaPackage:
        return MediaPackage(source_url=url, provider=self.name, items=[])


provider = _DummyProvider()
