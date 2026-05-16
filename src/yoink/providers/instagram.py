from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from yoink.core.models import MediaItem, MediaKind, MediaPackage
from yoink.downloader.runner import (
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable


class ProviderError(RuntimeError):
    """Permanent provider failure — pipeline logs and skips, never retries."""

    def __init__(self, message: str, *, url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


class ProviderTransientError(ProviderError):
    """Transient provider failure — eligible for pipeline retry."""


class MediaTooLarge(ProviderError):  # noqa: N818  # name locked in plan
    def __init__(self, path: Path, size_bytes: int, limit_bytes: int) -> None:
        super().__init__(
            f"media file {path.name} exceeds limit: {size_bytes}B > {limit_bytes}B",
        )
        self.path = path
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes


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

_STDERR_PEEK = 512


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


def _parse_gallery_dl_stdout(raw: bytes) -> list[dict[str, Any]]:
    if not raw or not raw.strip():
        return []
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        items: list[dict[str, Any]] = []
        for line in text.splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue
            try:
                obj = json.loads(line_stripped)
            except json.JSONDecodeError:
                continue
            items.extend(_extract_gallery_dl_meta(obj))
        return items
    return _extract_gallery_dl_meta(parsed)


def _extract_gallery_dl_meta(obj: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(obj, list):
        for entry in obj:
            if isinstance(entry, dict):
                out.append(entry)
            elif (
                isinstance(entry, list)
                and len(entry) >= 3
                and isinstance(entry[2], dict)
            ):
                out.append(entry[2])
    elif isinstance(obj, dict):
        out.append(obj)
    return out


def _resolve_gallery_dl_file(meta: dict[str, Any], workdir: Path) -> Path | None:
    fn = meta.get("filename")
    ext = meta.get("extension")
    if not isinstance(fn, str) or not fn:
        return None
    candidates: list[Path] = [workdir / fn]
    if isinstance(ext, str) and ext:
        candidates.append(workdir / f"{fn}.{ext}")
    for c in candidates:
        match = _safe_under(workdir, c)
        if match is not None:
            return match
    return None


def _resolve_yt_dlp_file(meta: dict[str, Any], workdir: Path) -> Path | None:
    requested = meta.get("requested_downloads")
    if isinstance(requested, list):
        for entry in requested:
            if not isinstance(entry, dict):
                continue
            for key in ("filepath", "_filename", "filename"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    candidate = Path(value)
                    if not candidate.is_absolute():
                        candidate = workdir / candidate
                    match = _safe_under(workdir, candidate)
                    if match is not None:
                        return match
    for key in ("_filename", "filename"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = workdir / candidate
            match = _safe_under(workdir, candidate)
            if match is not None:
                return match
    vid = meta.get("id")
    ext = meta.get("ext")
    if isinstance(vid, str) and isinstance(ext, str) and vid and ext:
        match = _safe_under(workdir, workdir / f"{vid}.{ext}")
        if match is not None:
            return match
    return None


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
        download_timeout_s: float = 90.0,
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
            "--dest",
            str(workdir),
            "--no-part",
            "--no-skip",
            "-o",
            "output.mode=null",
            "-o",
            "output.shorten=false",
            "--write-metadata=false",
            "--dump-json",
        ]
        cookies = self._effective_cookies()
        if cookies is not None:
            cmd.extend(["--cookies", str(cookies)])
        cmd.append(url)

        res = await self._runner(cmd, cwd=workdir, timeout_s=self._download_timeout_s)
        if res.returncode != 0:
            raise _ToolFailed(
                f"gallery-dl rc={res.returncode}: {res.stderr[:_STDERR_PEEK]}",
            )

        entries = _parse_gallery_dl_stdout(res.stdout)
        items: list[MediaItem] = []
        for meta in entries:
            path = _resolve_gallery_dl_file(meta, workdir)
            if path is None:
                continue
            ext_raw = meta.get("extension")
            ext = ext_raw if isinstance(ext_raw, str) and ext_raw else path.suffix.lstrip(".")
            kind, mime = _kind_for_ext(ext)
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
                    path=path,
                    kind=kind,
                    width=width,
                    height=height,
                    duration_s=duration,
                    mime=mime,
                ),
            )

        if not items:
            raise _ToolFailed("gallery-dl yielded zero items")
        return items

    async def _run_yt_dlp(self, url: str, workdir: Path) -> list[MediaItem]:
        cmd: list[str] = [
            "yt-dlp",
            "-o",
            f"{workdir}/%(id)s.%(ext)s",
            "--print-json",
            "--no-progress",
            "--no-warnings",
            url,
        ]
        cookies = self._effective_cookies()
        if cookies is not None:
            cmd.extend(["--cookies", str(cookies)])

        res = await self._runner(cmd, cwd=workdir, timeout_s=self._download_timeout_s)
        if res.returncode != 0:
            raise _ToolFailed(
                f"yt-dlp rc={res.returncode}: {res.stderr[:_STDERR_PEEK]}",
            )

        items: list[MediaItem] = []
        text = res.stdout.decode("utf-8", errors="replace")
        for line in text.splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue
            try:
                meta = json.loads(line_stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(meta, dict):
                continue
            path = _resolve_yt_dlp_file(meta, workdir)
            if path is None:
                continue
            ext_raw = meta.get("ext")
            ext = ext_raw if isinstance(ext_raw, str) and ext_raw else path.suffix.lstrip(".")
            kind, mime = _kind_for_ext(ext)
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
                    path=path,
                    kind=kind,
                    width=width,
                    height=height,
                    duration_s=duration,
                    mime=mime,
                ),
            )

        if not items:
            raise _ToolFailed("yt-dlp yielded zero items")
        return items

    def _enforce_size(self, path: Path) -> None:
        limit = self._effective_max_bytes()
        if limit is None:
            return
        size = path.stat().st_size
        if size > limit:
            raise MediaTooLarge(path, size, limit)

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


provider = InstagramProvider()
