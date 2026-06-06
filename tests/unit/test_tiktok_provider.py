from __future__ import annotations

import pytest

from yoink.providers.base import Provider
from yoink.providers.tiktok import TikTokProvider
from yoink.providers.tiktok import provider as module_provider


def test_module_provider_satisfies_protocol() -> None:
    assert isinstance(module_provider, Provider)
    assert module_provider.name == "tiktok"
    assert "tiktok.com" in module_provider.domains


def test_domains_cover_all_tiktok_hosts() -> None:
    p = TikTokProvider()
    assert p.domains == frozenset(
        {
            "tiktok.com",
            "www.tiktok.com",
            "m.tiktok.com",
            "vm.tiktok.com",
            "vt.tiktok.com",
        },
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@user/video/1234567890",
        "https://www.tiktok.com/@user/video/1234567890/",
        "https://tiktok.com/@user/video/1234567890",
        "https://www.tiktok.com/@user.name/photo/9876543210",
        "https://tiktok.com/@user/photo/9876543210/",
        "https://m.tiktok.com/@user/video/555",
        "https://vm.tiktok.com/ZMabc123/",
        "https://vm.tiktok.com/ZMabc123",
        "https://vt.tiktok.com/ZSdef456/",
        "https://www.tiktok.com/t/ZTtoken99/",
        "https://tiktok.com/t/ZTtoken99",
        "https://www.tiktok.com/v/1234567890",
    ],
)
def test_can_handle_accepts(url: str) -> None:
    assert TikTokProvider().can_handle(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@user",
        "https://www.tiktok.com/@user/",
        "https://www.tiktok.com/tag/funny",
        "https://www.tiktok.com/discover/cats",
        "https://www.tiktok.com/foryou",
        "https://www.tiktok.com/",
        "https://tiktok.com/@user/video/notanumber",
        "https://www.instagram.com/p/abc/",
        "https://example.com/@user/video/123",
        "not a url at all",
        "http://[invalid",
    ],
)
def test_can_handle_rejects(url: str) -> None:
    assert not TikTokProvider().can_handle(url)
