from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from yoink.core.errors import (
    MediaTooLarge,
    ProviderError,
    ProviderTransientError,
)
from yoink.core.models import MediaItem, MediaKind, MediaPackage
from yoink.downloader.runner import (
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
)
from yoink.downloader.safety import redact_text, redact_url

if TYPE_CHECKING:
    from collections.abc import Awaitable


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


# Extension → kind mapping. TikTok yields `.mp4` videos and `.jpg`/`.webp`
# slideshow images; the broader sets mirror the Instagram provider so both
# stay in step without coupling (duplicated intentionally — see plan Task 3).
_VIDEO_EXTS: frozenset[str] = frozenset({"mp4", "mov", "m4v", "webm", "mkv"})
_PHOTO_EXTS: frozenset[str] = frozenset({"jpg", "jpeg", "png", "webp", "heic", "heif"})
_ANIM_EXTS: frozenset[str] = frozenset({"gif"})
_MEDIA_EXTS: frozenset[str] = _VIDEO_EXTS | _PHOTO_EXTS | _ANIM_EXTS

_VIDEO_MIME: dict[str, str] = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "m4v": "video/x-m4v",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
}
_PHOTO_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "heic": "image/heic",
    "heif": "image/heif",
}
_ANIM_MIME: dict[str, str] = {"gif": "image/gif"}

_TRAILING_NUM_RE: re.Pattern[str] = re.compile(r"_(\d+)$")


def _kind_for_ext(ext: str) -> tuple[MediaKind, str | None]:
    e = ext.lower().lstrip(".")
    if e in _VIDEO_EXTS:
        return "video", _VIDEO_MIME.get(e)
    if e in _ANIM_EXTS:
        return "animation", _ANIM_MIME.get(e)
    if e in _PHOTO_EXTS:
        return "photo", _PHOTO_MIME.get(e)
    return "document", None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _sort_key(path: Path) -> tuple[int, int, str]:
    # Sort by directory depth, then by trailing numeric suffix as int
    # (so `_10.jpg` follows `_9.jpg`, not `_1.jpg`), then lexicographic.
    # TikTok photo slideshows arrive as `<id>_1.jpg`, `<id>_2.jpg`, ... so
    # numeric ordering preserves the author's intended slide sequence.
    match = _TRAILING_NUM_RE.search(path.stem)
    num = int(match.group(1)) if match else -1
    return (len(path.parts), num, str(path))


def _safe_under(workdir: Path, candidate: Path) -> Path | None:
    try:
        resolved = candidate.resolve()
        root = workdir.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    if not resolved.exists() or not resolved.is_file():
        return None
    return resolved


def _read_sidecar(media_path: Path) -> dict[str, Any]:
    # gallery-dl `--write-metadata` writes `<name>.json`; yt-dlp
    # `--write-info-json` writes `<stem>.info.json`.
    candidates = (
        media_path.parent / (media_path.name + ".json"),
        media_path.parent / (media_path.stem + ".info.json"),
    )
    for sidecar in candidates:
        if not sidecar.is_file():
            continue
        try:
            parsed = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


async def _ffprobe_dims(
    path: Path,
    *,
    runner: _Runner,
    timeout_s: float = 10.0,
) -> dict[str, int]:
    if shutil.which("ffprobe") is None:
        return {}
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        res = await runner(cmd, cwd=path.parent, timeout_s=timeout_s)
    except SubprocessTimeoutError:
        return {}
    if res.returncode != 0:
        return {}
    try:
        parsed = json.loads(res.stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return {}
    streams = parsed.get("streams") if isinstance(parsed, dict) else None
    if not isinstance(streams, list) or not streams:
        return {}
    first = streams[0]
    if not isinstance(first, dict):
        return {}
    out: dict[str, int] = {}
    w = _coerce_int(first.get("width"))
    h = _coerce_int(first.get("height"))
    if w is not None:
        out["width"] = w
    if h is not None:
        out["height"] = h
    dur_raw = first.get("duration")
    if isinstance(dur_raw, str):
        with contextlib.suppress(ValueError):
            out["duration_s"] = int(float(dur_raw))
    else:
        dur_int = _coerce_int(dur_raw)
        if dur_int is not None:
            out["duration_s"] = dur_int
    return out


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
        workdir.mkdir(parents=True, exist_ok=True)
        # Purge any leftover artifacts from a prior failed attempt so the
        # retry doesn't pick up truncated/half-written files as success.
        self._purge_workdir(workdir)

        # yt-dlp is the primary TikTok extractor; gallery-dl is the fallback
        # (inverts the Instagram order — yt-dlp has the stronger TikTok
        # extractor and resolves short links cleanly).
        primary_err: str | None = None
        primary_transient = False
        items: list[MediaItem] = []
        try:
            items = await self._run_yt_dlp(url, workdir)
        except _ToolFailed as exc:
            primary_err = str(exc)
            primary_transient = exc.transient

        if not items:
            # Purge yt-dlp partials so gallery-dl's _collect_items doesn't
            # pick up truncated/half-written files as successful artifacts.
            self._purge_workdir(workdir)
            try:
                items = await self._run_gallery_dl(url, workdir)
            except _ToolFailed as exc:
                # If either extractor failed transiently, the combined fetch
                # is retry-eligible: backoff + retry may let the transient
                # tool succeed.
                redacted = redact_url(url)
                if primary_transient or exc.transient:
                    raise ProviderTransientError(
                        "extractor transient-failed for "
                        f"{redacted}: yt-dlp={primary_err!r}, gallery-dl={exc!s}",
                        url=url,
                    ) from exc
                raise ProviderError(
                    "both extractors failed for "
                    f"{redacted}: yt-dlp={primary_err!r}, gallery-dl={exc!s}",
                    url=url,
                ) from exc

        if not items:
            raise ProviderError(
                f"no media items extracted from {redact_url(url)}", url=url
            )

        for item in items:
            self._enforce_size(item.path)

        return MediaPackage(
            source_url=url,
            provider=self.name,
            items=items,
            caption=None,
        )

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

    async def _run_gallery_dl(self, url: str, workdir: Path) -> list[MediaItem]:
        # gallery-dl is the TikTok fallback: it occasionally extracts a post
        # yt-dlp's extractor chokes on. No cookies — public posts only.
        cmd: list[str] = [
            "gallery-dl",
            "--config-ignore",
            "-D",
            str(workdir),
            "--no-part",
            "--no-skip",
            "-o",
            "output.mode=null",
            "-o",
            "output.shorten=false",
            "--write-metadata",
            "--",
            url,
        ]

        timeout = self._effective_timeout()
        try:
            res = await self._runner(cmd, cwd=workdir, timeout_s=timeout)
        except SubprocessTimeoutError as exc:
            raise _ToolFailed(
                f"gallery-dl timeout after {timeout}s", transient=True
            ) from exc
        if res.returncode != 0:
            raise _ToolFailed(
                f"gallery-dl rc={res.returncode}: {redact_text(res.stderr[:_STDERR_PEEK])}",
                transient=_is_transient_stderr(res.stderr),
            )

        items = await self._collect_items(workdir)
        if not items:
            raise _ToolFailed("gallery-dl yielded zero items")
        return items

    async def _collect_items(self, workdir: Path) -> list[MediaItem]:
        media_paths: list[Path] = []
        for p in workdir.rglob("*"):
            if not p.is_file():
                continue
            if p.name.startswith("."):
                continue
            if p.suffix.lstrip(".").lower() not in _MEDIA_EXTS:
                continue
            media_paths.append(p)
        media_paths.sort(key=_sort_key)

        items: list[MediaItem] = []
        for path in media_paths:
            match = _safe_under(workdir, path)
            if match is None:
                continue
            ext = path.suffix.lstrip(".")
            kind, mime = _kind_for_ext(ext)

            meta = _read_sidecar(path)
            width = _coerce_int(meta.get("width"))
            height = _coerce_int(meta.get("height"))
            duration = _coerce_int(meta.get("duration"))

            if kind == "video" and self._probe_video_dims and (
                width is None or height is None or duration is None
            ):
                probed = await _ffprobe_dims(path, runner=self._runner)
                if probed:
                    width = width if width is not None else probed.get("width")
                    height = height if height is not None else probed.get("height")
                    duration = (
                        duration if duration is not None else probed.get("duration_s")
                    )

            items.append(
                MediaItem(
                    path=match,
                    kind=kind,
                    width=width,
                    height=height,
                    duration_s=duration,
                    mime=mime,
                ),
            )
        return items

    def _enforce_size(self, path: Path) -> None:
        limit = self._effective_max_bytes()
        if limit is None:
            return
        size = path.stat().st_size
        if size > limit:
            raise MediaTooLarge.from_size(path, size, limit)

    @staticmethod
    def _purge_workdir(workdir: Path) -> None:
        # Remove stale artifacts from a prior failed attempt so a retry does
        # not pick up truncated/half-written files as success. Preserve the
        # pipeline `.heartbeat` sentinel if it ever coincides with a workdir
        # (per-job dirs won't, but the guard is cheap and matches the
        # runtime invariant documented in CLAUDE.md).
        if not workdir.exists():
            return
        for entry in workdir.iterdir():
            if entry.name == ".heartbeat":
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    entry.unlink()

    def _effective_max_bytes(self) -> int | None:
        if self._max_file_bytes is not None:
            return self._max_file_bytes
        env_value = os.environ.get("YOINK_MAX_FILE_MB")
        if not env_value:
            return None
        try:
            return int(env_value) * 1024 * 1024
        except ValueError:
            return None

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
