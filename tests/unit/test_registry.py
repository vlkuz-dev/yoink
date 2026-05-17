from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from yoink.core.models import MediaPackage
from yoink.core.registry import ProviderRegistry


@dataclass
class FakeProvider:
    name: str = "fake"
    domains: frozenset[str] = field(default_factory=lambda: frozenset({"example.com"}))
    handle: bool = True

    def can_handle(self, url: str) -> bool:
        return self.handle

    async def fetch(self, url: str, workdir: Path) -> MediaPackage:
        return MediaPackage(source_url=url, provider=self.name, items=[])


def test_register_and_find_by_url() -> None:
    registry = ProviderRegistry()
    provider = FakeProvider()
    registry.register(provider)

    found = registry.find("https://example.com/some/path")
    assert found is provider


def test_find_returns_none_for_unknown_host() -> None:
    registry = ProviderRegistry()
    registry.register(FakeProvider())

    assert registry.find("https://other.test/whatever") is None


def test_find_returns_none_when_no_host() -> None:
    registry = ProviderRegistry()
    registry.register(FakeProvider())

    assert registry.find("not a url") is None


def test_find_strips_www_and_is_case_insensitive() -> None:
    registry = ProviderRegistry()
    provider = FakeProvider(domains=frozenset({"example.com"}))
    registry.register(provider)

    assert registry.find("https://www.EXAMPLE.com/x") is provider
    assert registry.find("HTTPS://www.example.com/x") is provider


def test_find_matches_when_provider_registers_www_prefix() -> None:
    registry = ProviderRegistry()
    provider = FakeProvider(domains=frozenset({"www.example.com"}))
    registry.register(provider)

    assert registry.find("https://example.com/x") is provider


def test_find_returns_none_when_can_handle_rejects() -> None:
    registry = ProviderRegistry()
    registry.register(FakeProvider(handle=False))

    assert registry.find("https://example.com/x") is None


def test_register_rejects_non_provider() -> None:
    registry = ProviderRegistry()
    with pytest.raises(TypeError):
        registry.register(object())  # type: ignore[arg-type]


def test_autodiscover_picks_up_fixture_provider() -> None:
    registry = ProviderRegistry.autodiscover(package="tests.fixtures")

    found = registry.find("https://dummy.test/post/123")
    assert found is not None
    assert found.name == "dummy"


def test_autodiscover_skips_non_provider_module() -> None:
    registry = ProviderRegistry.autodiscover(package="tests.fixtures")

    assert registry.find("https://no-such-domain.test/") is None


def test_autodiscover_on_empty_providers_package_returns_empty_registry() -> None:
    registry = ProviderRegistry.autodiscover()

    assert registry.find("https://example.com/x") is None
