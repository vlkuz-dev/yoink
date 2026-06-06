from __future__ import annotations

import pytest

from yoink.core.registry import ProviderRegistry
from yoink.downloader.safety import UnsafeURLError, validate_url

# All hostnames the TikTok provider claims, in their post-normalization form
# (`core.registry._normalize_host` lowercases and strips a single leading
# `www.`, so `www.tiktok.com` collapses to `tiktok.com`).
_TIKTOK_NORMALIZED_HOSTS = frozenset(
    {"tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"},
)


@pytest.fixture
def registry() -> ProviderRegistry:
    return ProviderRegistry.autodiscover()


def test_autodiscover_registers_tiktok_provider(registry: ProviderRegistry) -> None:
    found = registry.find("https://www.tiktok.com/@u/video/1")
    assert found is not None
    assert found.name == "tiktok"


def test_known_domains_contains_all_tiktok_hosts(registry: ProviderRegistry) -> None:
    # `www.tiktok.com` is registered too, but normalizes to `tiktok.com`, so
    # the registry exposes four distinct keys covering all five claimed hosts.
    assert registry.known_domains >= _TIKTOK_NORMALIZED_HOSTS


def test_find_returns_tiktok_for_full_video_url(registry: ProviderRegistry) -> None:
    found = registry.find("https://www.tiktok.com/@user/video/1234567890")
    assert found is not None
    assert found.name == "tiktok"


def test_find_returns_tiktok_for_photo_url(registry: ProviderRegistry) -> None:
    found = registry.find("https://www.tiktok.com/@user/photo/1234567890")
    assert found is not None
    assert found.name == "tiktok"


def test_find_returns_tiktok_for_short_link(registry: ProviderRegistry) -> None:
    for url in (
        "https://vm.tiktok.com/ZMabc123/",
        "https://vt.tiktok.com/ZMxyz789/",
        "https://www.tiktok.com/t/ZMtoken/",
    ):
        found = registry.find(url)
        assert found is not None, url
        assert found.name == "tiktok", url


def test_find_rejects_bare_profile(registry: ProviderRegistry) -> None:
    # Host is known but path isn't a media post → can_handle rejects → no provider.
    assert registry.find("https://www.tiktok.com/@user") is None


def test_tiktok_host_passes_validate_url_under_known_domains_allowlist(
    registry: ProviderRegistry,
) -> None:
    # The allowlist the pipeline builds when YOINK_ALLOWLIST_MODE=true is the
    # union of every provider's domains via `known_domains`. A TikTok URL must
    # survive `validate_url` against that allowlist. DNS resolution is off, as
    # in the pipeline (`resolve_dns=False`); host/scheme/port checks still apply.
    allowlist = registry.known_domains
    for url in (
        "https://www.tiktok.com/@user/video/1234567890",
        "https://m.tiktok.com/@user/photo/1234567890",
        "https://vm.tiktok.com/ZMabc123/",
        "https://vt.tiktok.com/ZMxyz789/",
    ):
        validated = validate_url(url, allowlist, resolve_dns=False)
        assert validated.host.endswith("tiktok.com"), url


def test_non_tiktok_host_rejected_by_known_domains_allowlist(
    registry: ProviderRegistry,
) -> None:
    allowlist = registry.known_domains
    with pytest.raises(UnsafeURLError):
        validate_url("https://evil.example/x", allowlist, resolve_dns=False)
