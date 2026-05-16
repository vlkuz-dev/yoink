from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from yoink.core.errors import MediaTooLarge, ProviderError, ProviderTransientError
from yoink.core.models import MediaItem, MediaKind, MediaPackage
from yoink.downloader.runner import (
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable


__all__ = [
    "InstagramProvider",
    "MediaTooLarge",
    "ProviderError",
    "ProviderTransientError",
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

_IG_HOSTS: frozenset[str] = frozenset({"instagram.com", "instagr.am"})
_IG_PATH_RE: re.Pattern[str] = re.compile(r"^/(p|reel|tv|stories)/", re.IGNORECASE)
_TRAILING_NUM_RE: re.Pattern[str] = re.compile(r"_(\d+)$")

_STDERR_PEEK = 512
_DEFAULT_DOWNLOAD_TIMEOUT_S = 90.0


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


class InstagramProvider:
    name: str = "instagram"
    domains: frozenset[str] = frozenset(
        {"instagram.com", "www.instagram.com", "instagr.am"},
    )

    def __init__(
        self,
        *,
        runner: _Runner | None = None,
        cookies_file: Path | None = None,
        max_file_bytes: int | None = None,
        download_timeout_s: float | None = None,
        probe_video_dims: bool = True,
    ) -> None:
        self._runner: _Runner = runner if runner is not None else run_subprocess
        self._cookies_file = cookies_file
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
        if host not in _IG_HOSTS:
            return False
        return _IG_PATH_RE.match(parts.path or "") is not None

    async def fetch(self, url: str, workdir: Path) -> MediaPackage:
        workdir.mkdir(parents=True, exist_ok=True)

        primary_err: str | None = None
        items: list[MediaItem] = []
        try:
            items = await self._run_gallery_dl(url, workdir)
        except _ToolFailed as exc:
            primary_err = str(exc)

        if not items:
            # Purge gallery-dl partials so yt-dlp's _collect_items doesn't
            # pick up truncated/half-written files as successful artifacts.
            self._purge_workdir(workdir)
            try:
                items = await self._run_yt_dlp(url, workdir)
            except _ToolFailed as exc:
                raise ProviderError(
                    "both extractors failed for "
                    f"{url}: gallery-dl={primary_err!r}, yt-dlp={exc!s}",
                    url=url,
                ) from exc

        if not items:
            raise ProviderError(f"no media items extracted from {url}", url=url)

        for item in items:
            self._enforce_size(item.path)

        return MediaPackage(
            source_url=url,
            provider=self.name,
            items=items,
            caption=None,
        )

    async def _run_gallery_dl(self, url: str, workdir: Path) -> list[MediaItem]:
        cmd: list[str] = [
            "gallery-dl",
            "--no-config",
            "-D",
            str(workdir),
            "--no-part",
            "--no-skip",
            "-o",
            "output.mode=null",
            "-o",
            "output.shorten=false",
            "--write-metadata",
        ]
        cookies = self._effective_cookies()
        if cookies is not None:
            cmd.extend(["--cookies", str(cookies)])
        cmd.extend(["--", url])

        timeout = self._effective_timeout()
        try:
            res = await self._runner(cmd, cwd=workdir, timeout_s=timeout)
        except SubprocessTimeoutError as exc:
            raise _ToolFailed(f"gallery-dl timeout after {timeout}s") from exc
        if res.returncode != 0:
            raise _ToolFailed(
                f"gallery-dl rc={res.returncode}: {res.stderr[:_STDERR_PEEK]}",
            )

        items = await self._collect_items(workdir)
        if not items:
            raise _ToolFailed("gallery-dl yielded zero items")
        return items

    async def _run_yt_dlp(self, url: str, workdir: Path) -> list[MediaItem]:
        cmd: list[str] = [
            "yt-dlp",
            "--ignore-config",
            "-o",
            f"{workdir}/%(id)s.%(ext)s",
            "--no-progress",
            "--no-warnings",
            "--write-info-json",
        ]
        cookies = self._effective_cookies()
        if cookies is not None:
            cmd.extend(["--cookies", str(cookies)])
        cmd.extend(["--", url])

        timeout = self._effective_timeout()
        try:
            res = await self._runner(cmd, cwd=workdir, timeout_s=timeout)
        except SubprocessTimeoutError as exc:
            raise _ToolFailed(f"yt-dlp timeout after {timeout}s") from exc
        if res.returncode != 0:
            raise _ToolFailed(
                f"yt-dlp rc={res.returncode}: {res.stderr[:_STDERR_PEEK]}",
            )

        items = await self._collect_items(workdir)
        if not items:
            raise _ToolFailed("yt-dlp yielded zero items")
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

    def configure(
        self,
        *,
        cookies_file: Path | None = None,
        max_file_bytes: int | None = None,
        download_timeout_s: float | None = None,
    ) -> None:
        """Apply runtime settings to the autodiscovered singleton.

        Called from `__main__` after autodiscover so values loaded from
        `.env` via pydantic-settings reach the provider — the singleton
        is constructed at import time before Settings exists, and
        pydantic-settings does not export `.env` into `os.environ`.
        """
        if cookies_file is not None:
            self._cookies_file = cookies_file
        if max_file_bytes is not None:
            self._max_file_bytes = max_file_bytes
        if download_timeout_s is not None:
            self._download_timeout_s = download_timeout_s

    @staticmethod
    def _purge_workdir(workdir: Path) -> None:
        if not workdir.exists():
            return
        for entry in workdir.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    entry.unlink()

    def _effective_cookies(self) -> Path | None:
        if self._cookies_file is not None:
            return self._cookies_file
        env_value = os.environ.get("YOINK_IG_COOKIES_FILE")
        return Path(env_value) if env_value else None

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


provider = InstagramProvider()
