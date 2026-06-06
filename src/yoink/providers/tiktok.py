from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from yoink.core.errors import (
    MediaTooLarge,
    ProviderError,
    ProviderTransientError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from yoink.core.models import MediaPackage


__all__ = [
    "MediaTooLarge",
    "ProviderError",
    "ProviderTransientError",
    "TikTokProvider",
    "provider",
]


# Short-link hosts whose path is an opaque redirect token, NOT `/video/` or
# `/photo/`. Accept these by host alone; yt-dlp / gallery-dl follow the
# redirect internally, so `fetch` passes the URL through unchanged.
_TIKTOK_SHORT_HOSTS: frozenset[str] = frozenset({"vm.tiktok.com", "vt.tiktok.com"})

# Hosts that require a media-path match. (`www.` is stripped during
# normalization, so `www.tiktok.com` collapses to `tiktok.com` here — matching
# `core.registry._normalize_host` so dispatch and `can_handle` agree.)
_TIKTOK_PATH_HOSTS: frozenset[str] = frozenset({"tiktok.com", "m.tiktok.com"})

# Accept full video/photo posts (`/@user/video/<id>`, `/@user/photo/<id>`),
# the `/t/<token>/` short form, and the legacy `/v/<id>` form. Reject bare
# profile (`/@user`), `/tag/...`, `/discover/...`, `/foryou`, etc.
_TIKTOK_PATH_RE: re.Pattern[str] = re.compile(
    r"^/(@[^/]+/(video|photo)/\d+|t/|v/\d+)",
    re.IGNORECASE,
)


class TikTokProvider:
    name: str = "tiktok"
    domains: frozenset[str] = frozenset(
        {
            "tiktok.com",
            "www.tiktok.com",
            "m.tiktok.com",
            "vm.tiktok.com",
            "vt.tiktok.com",
        },
    )

    def can_handle(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host in _TIKTOK_SHORT_HOSTS:
            return True
        if host in _TIKTOK_PATH_HOSTS:
            return _TIKTOK_PATH_RE.match(parts.path or "") is not None
        return False

    async def fetch(self, url: str, workdir: Path) -> MediaPackage:
        raise NotImplementedError  # implemented in a later task


provider = TikTokProvider()
