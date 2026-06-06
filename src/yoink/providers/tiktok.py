from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from yoink.core.errors import (
    MediaTooLarge,
    ProviderError,
    ProviderTransientError,
)
from yoink.downloader.runner import (
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
)
from yoink.downloader.safety import redact_text

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

    from yoink.core.models import MediaItem, MediaPackage


__all__ = [
    "MediaTooLarge",
    "ProviderError",
    "ProviderTransientError",
    "TikTokProvider",
    "provider",
]


class _Runner(Protocol):
    def __call__(
        self,
        cmd: list[str],
        *,
        cwd: Path,
        timeout_s: float,
        env: dict[str, str] | None = ...,
        stderr_cap_bytes: int = ...,
    ) -> Awaitable[SubprocessResult]: ...


class _ToolFailed(Exception):  # noqa: N818  # internal sentinel, not surfaced
    """Internal: one extractor exhausted its options; try the next."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


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

_STDERR_PEEK = 512
_DEFAULT_DOWNLOAD_TIMEOUT_S = 90.0

# Substrings (case-insensitive) in extractor stderr that indicate a
# transient failure (rate limit, network glitch, upstream 5xx). Matching
# any of these promotes a non-zero subprocess exit from permanent to
# retry-eligible so the pipeline's `ProviderTransientError` backoff applies.
# Mirrors the Instagram provider's marker list — TikTok has no auth/cookie
# failure mode, so no cookie-dead markers are needed.
_TRANSIENT_STDERR_MARKERS: tuple[str, ...] = (
    "http error 429",
    "429 too many requests",
    "429 client error",
    "rate limit",
    "rate-limit",
    "rate limited",
    "too many requests",
    "http error 5",
    "http error 408",
    "http error 425",
    "connection reset",
    "connection refused",
    "connection aborted",
    "network is unreachable",
    "temporarily unavailable",
    "service unavailable",
    "remote end closed connection",
    "timed out",
    "read timeout",
    "name or service not known",
)


def _is_transient_stderr(stderr: str) -> bool:
    text = stderr.lower()
    return any(marker in text for marker in _TRANSIENT_STDERR_MARKERS)


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

    def __init__(
        self,
        *,
        runner: _Runner | None = None,
        max_file_bytes: int | None = None,
        download_timeout_s: float | None = None,
        probe_video_dims: bool = True,
    ) -> None:
        self._runner: _Runner = runner if runner is not None else run_subprocess
        self._max_file_bytes = max_file_bytes
        self._download_timeout_s = download_timeout_s
        self._probe_video_dims = probe_video_dims

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

    async def _run_yt_dlp(self, url: str, workdir: Path) -> list[MediaItem]:
        # yt-dlp is the primary TikTok extractor: it has the stronger
        # extractor and resolves short links (`vm.`/`vt.tiktok.com`) cleanly.
        cmd: list[str] = [
            "yt-dlp",
            "--ignore-config",
            "-o",
            f"{workdir}/%(id)s.%(ext)s",
            "--no-progress",
            "--no-warnings",
            "--write-info-json",
            "--",
            url,
        ]

        timeout = self._effective_timeout()
        try:
            res = await self._runner(cmd, cwd=workdir, timeout_s=timeout)
        except SubprocessTimeoutError as exc:
            raise _ToolFailed(
                f"yt-dlp timeout after {timeout}s", transient=True
            ) from exc
        if res.returncode != 0:
            raise _ToolFailed(
                f"yt-dlp rc={res.returncode}: {redact_text(res.stderr[:_STDERR_PEEK])}",
                transient=_is_transient_stderr(res.stderr),
            )

        items = await self._collect_items(workdir)
        if not items:
            raise _ToolFailed("yt-dlp yielded zero items")
        return items

    async def _collect_items(self, workdir: Path) -> list[MediaItem]:
        # Stub: real glob/sort/sidecar/ffprobe collection lands in Task 3.
        return []

    def _effective_timeout(self) -> float:
        if self._download_timeout_s is not None:
            return self._download_timeout_s
        env_value = os.environ.get("YOINK_DOWNLOAD_TIMEOUT_S")
        if env_value:
            try:
                return float(env_value)
            except ValueError:
                pass
        return _DEFAULT_DOWNLOAD_TIMEOUT_S


provider = TikTokProvider()
