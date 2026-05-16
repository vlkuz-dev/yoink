from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from yoink.downloader.runner import SubprocessResult
from yoink.providers.base import Provider
from yoink.providers.instagram import (
    InstagramProvider,
    MediaTooLarge,
    ProviderError,
)
from yoink.providers.instagram import provider as module_provider

SideEffect = Callable[[list[str], Path], None]


class _Recorder:
    def __init__(
        self,
        scripts: list[SubprocessResult | Exception],
        *,
        side_effect: SideEffect | None = None,
    ) -> None:
        self._scripts = list(scripts)
        self.calls: list[list[str]] = []
        self._side_effect = side_effect

    async def __call__(
        self,
        cmd: list[str],
        *,
        cwd: Path,
        timeout_s: float,
        env: dict[str, str] | None = None,
        stderr_cap_bytes: int = 64 * 1024,
    ) -> SubprocessResult:
        self.calls.append(list(cmd))
        if self._side_effect is not None:
            self._side_effect(cmd, cwd)
        if not self._scripts:
            raise AssertionError(f"no scripted response for cmd: {cmd!r}")
        nxt = self._scripts.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _result(
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: str = "",
) -> SubprocessResult:
    return SubprocessResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_s=0.001,
    )


def _make_file(path: Path, *, size: int = 8) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _write_sidecar(media_path: Path, meta: dict[str, object]) -> None:
    sidecar = media_path.parent / (media_path.name + ".json")
    sidecar.write_text(json.dumps(meta), encoding="utf-8")


def test_can_handle_post() -> None:
    p = InstagramProvider()
    assert p.can_handle("https://www.instagram.com/p/ABCDEF/")
    assert p.can_handle("https://instagram.com/p/ABCDEF")
    assert p.can_handle("https://instagr.am/p/ABCDEF/")


def test_can_handle_reel_tv_stories() -> None:
    p = InstagramProvider()
    assert p.can_handle("https://www.instagram.com/reel/XYZ/")
    assert p.can_handle("https://www.instagram.com/tv/ABC123/")
    assert p.can_handle("https://www.instagram.com/stories/user/12345/")


def test_can_handle_rejects_non_media_paths() -> None:
    p = InstagramProvider()
    assert not p.can_handle("https://www.instagram.com/explore/")
    assert not p.can_handle("https://www.instagram.com/")
    assert not p.can_handle("https://example.com/p/abc/")
    assert not p.can_handle("https://www.instagram.com/accounts/login/")


def test_module_provider_satisfies_protocol() -> None:
    assert isinstance(module_provider, Provider)
    assert module_provider.name == "instagram"
    assert "instagram.com" in module_provider.domains


async def test_single_photo_post(tmp_path: Path) -> None:
    def side(cmd: list[str], cwd: Path) -> None:
        if cmd[0] == "gallery-dl":
            f = tmp_path / "111_1.jpg"
            _make_file(f)
            _write_sidecar(f, {"width": 1080, "height": 1080})

    rec = _Recorder([_result()], side_effect=side)
    p = InstagramProvider(runner=rec, probe_video_dims=False)
    pkg = await p.fetch("https://www.instagram.com/p/AAA/", tmp_path)

    assert pkg.provider == "instagram"
    assert pkg.source_url == "https://www.instagram.com/p/AAA/"
    assert len(pkg.items) == 1
    item = pkg.items[0]
    assert item.kind == "photo"
    assert item.mime == "image/jpeg"
    assert item.width == 1080
    assert item.height == 1080
    assert item.path.name == "111_1.jpg"


async def test_carousel_three_items_stable_order(tmp_path: Path) -> None:
    def side(cmd: list[str], cwd: Path) -> None:
        if cmd[0] == "gallery-dl":
            for i in (1, 2, 3):
                f = tmp_path / f"1234567890_{i}.jpg"
                _make_file(f)
                _write_sidecar(f, {"width": 1080, "height": 1350})

    rec = _Recorder([_result()], side_effect=side)
    p = InstagramProvider(runner=rec, probe_video_dims=False)
    pkg = await p.fetch("https://www.instagram.com/p/ABCDEF/", tmp_path)

    assert len(pkg.items) == 3
    names = [it.path.name for it in pkg.items]
    assert names == [
        "1234567890_1.jpg",
        "1234567890_2.jpg",
        "1234567890_3.jpg",
    ]
    for item in pkg.items:
        assert item.kind == "photo"
        assert item.mime == "image/jpeg"
        assert item.width == 1080
        assert item.height == 1350


async def test_reel_returns_video(tmp_path: Path) -> None:
    def side(cmd: list[str], cwd: Path) -> None:
        if cmd[0] == "gallery-dl":
            f = tmp_path / "reel1.mp4"
            _make_file(f)
            _write_sidecar(f, {"width": 720, "height": 1280, "duration": 15})

    rec = _Recorder([_result()], side_effect=side)
    p = InstagramProvider(runner=rec, probe_video_dims=False)
    pkg = await p.fetch("https://www.instagram.com/reel/AAA/", tmp_path)

    assert len(pkg.items) == 1
    item = pkg.items[0]
    assert item.kind == "video"
    assert item.mime == "video/mp4"
    assert item.width == 720
    assert item.height == 1280
    assert item.duration_s == 15


async def test_gallery_dl_rc1_triggers_yt_dlp_fallback(tmp_path: Path) -> None:
    def side(cmd: list[str], cwd: Path) -> None:
        if cmd[0] == "yt-dlp":
            f = tmp_path / "vid42.mp4"
            _make_file(f)
            _write_sidecar(f, {"width": 720, "height": 1280, "duration": 8})

    rec = _Recorder(
        [
            _result(returncode=1, stderr="login required"),
            _result(),
        ],
        side_effect=side,
    )
    p = InstagramProvider(runner=rec, probe_video_dims=False)
    pkg = await p.fetch("https://www.instagram.com/reel/XYZ/", tmp_path)

    assert [c[0] for c in rec.calls] == ["gallery-dl", "yt-dlp"]
    assert len(pkg.items) == 1
    assert pkg.items[0].kind == "video"
    assert pkg.items[0].duration_s == 8


async def test_gallery_dl_zero_items_triggers_fallback(tmp_path: Path) -> None:
    def side(cmd: list[str], cwd: Path) -> None:
        if cmd[0] == "yt-dlp":
            _make_file(tmp_path / "vid88.mp4")

    rec = _Recorder(
        [
            _result(),  # gallery-dl rc=0 but no files
            _result(),
        ],
        side_effect=side,
    )
    p = InstagramProvider(runner=rec, probe_video_dims=False)
    pkg = await p.fetch("https://www.instagram.com/reel/QQQ/", tmp_path)

    assert [c[0] for c in rec.calls] == ["gallery-dl", "yt-dlp"]
    assert len(pkg.items) == 1


async def test_both_extractors_fail_raises_provider_error(tmp_path: Path) -> None:
    rec = _Recorder(
        [
            _result(returncode=1, stderr="boom"),
            _result(returncode=2, stderr="kaboom"),
        ],
    )
    p = InstagramProvider(runner=rec, probe_video_dims=False)
    with pytest.raises(ProviderError) as ei:
        await p.fetch("https://www.instagram.com/p/ZZZ/", tmp_path)
    msg = str(ei.value)
    assert "gallery-dl" in msg
    assert "yt-dlp" in msg


async def test_media_too_large_raised(tmp_path: Path) -> None:
    def side(cmd: list[str], cwd: Path) -> None:
        if cmd[0] == "gallery-dl":
            _make_file(tmp_path / "big.jpg", size=4096)

    rec = _Recorder([_result()], side_effect=side)
    p = InstagramProvider(runner=rec, probe_video_dims=False, max_file_bytes=128)

    with pytest.raises(MediaTooLarge) as ei:
        await p.fetch("https://www.instagram.com/p/HUGE/", tmp_path)
    assert ei.value.size_bytes == 4096
    assert ei.value.limit_bytes == 128


async def test_cookies_passed_to_gallery_dl(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("c", encoding="utf-8")

    def side(cmd: list[str], cwd: Path) -> None:
        if cmd[0] == "gallery-dl":
            _make_file(tmp_path / "x.jpg")

    rec = _Recorder([_result()], side_effect=side)
    p = InstagramProvider(runner=rec, probe_video_dims=False, cookies_file=cookies)
    await p.fetch("https://www.instagram.com/p/CCC/", tmp_path)
    gd = rec.calls[0]
    assert "--cookies" in gd
    assert str(cookies) in gd


async def test_gallery_dl_uses_security_flags(tmp_path: Path) -> None:
    def side(cmd: list[str], cwd: Path) -> None:
        if cmd[0] == "gallery-dl":
            _make_file(tmp_path / "x.jpg")

    rec = _Recorder([_result()], side_effect=side)
    p = InstagramProvider(runner=rec, probe_video_dims=False)
    url = "https://www.instagram.com/p/SEC/"
    await p.fetch(url, tmp_path)

    cmd = rec.calls[0]
    assert cmd[0] == "gallery-dl"
    assert "--no-config" in cmd
    # `--` separator must precede the URL (argument-injection guard)
    assert cmd[-2] == "--"
    assert cmd[-1] == url


async def test_yt_dlp_uses_security_flags(tmp_path: Path) -> None:
    def side(cmd: list[str], cwd: Path) -> None:
        if cmd[0] == "yt-dlp":
            _make_file(tmp_path / "vidx.mp4")

    rec = _Recorder(
        [
            _result(returncode=1, stderr="login required"),
            _result(),
        ],
        side_effect=side,
    )
    p = InstagramProvider(runner=rec, probe_video_dims=False)
    url = "https://www.instagram.com/reel/SEC/"
    await p.fetch(url, tmp_path)

    yt_cmd = rec.calls[1]
    assert yt_cmd[0] == "yt-dlp"
    assert "--ignore-config" in yt_cmd
    assert yt_cmd[-2] == "--"
    assert yt_cmd[-1] == url


async def test_skips_json_sidecars_and_dotfiles(tmp_path: Path) -> None:
    def side(cmd: list[str], cwd: Path) -> None:
        if cmd[0] == "gallery-dl":
            f = tmp_path / "p1.jpg"
            _make_file(f)
            _write_sidecar(f, {"width": 800, "height": 600})
            # stray dotfile and a non-media file should be ignored
            (tmp_path / ".cache").write_bytes(b"meta")
            (tmp_path / "notes.txt").write_bytes(b"hi")

    rec = _Recorder([_result()], side_effect=side)
    p = InstagramProvider(runner=rec, probe_video_dims=False)
    pkg = await p.fetch("https://www.instagram.com/p/MIX/", tmp_path)

    assert len(pkg.items) == 1
    assert pkg.items[0].path.name == "p1.jpg"


async def test_download_timeout_env_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_timeouts: list[float] = []

    async def fake_runner(
        cmd: list[str],
        *,
        cwd: Path,
        timeout_s: float,
        env: dict[str, str] | None = None,
        stderr_cap_bytes: int = 64 * 1024,
    ) -> SubprocessResult:
        seen_timeouts.append(timeout_s)
        if cmd[0] == "gallery-dl":
            _make_file(tmp_path / "t.jpg")
        return SubprocessResult(returncode=0, stdout=b"", stderr="", duration_s=0.0)

    monkeypatch.setenv("YOINK_DOWNLOAD_TIMEOUT_S", "12")
    p = InstagramProvider(runner=fake_runner, probe_video_dims=False)
    await p.fetch("https://www.instagram.com/p/TO/", tmp_path)

    assert seen_timeouts == [12.0]


async def test_ctor_timeout_overrides_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_timeouts: list[float] = []

    async def fake_runner(
        cmd: list[str],
        *,
        cwd: Path,
        timeout_s: float,
        env: dict[str, str] | None = None,
        stderr_cap_bytes: int = 64 * 1024,
    ) -> SubprocessResult:
        seen_timeouts.append(timeout_s)
        if cmd[0] == "gallery-dl":
            _make_file(tmp_path / "t.jpg")
        return SubprocessResult(returncode=0, stdout=b"", stderr="", duration_s=0.0)

    monkeypatch.setenv("YOINK_DOWNLOAD_TIMEOUT_S", "999")
    p = InstagramProvider(
        runner=fake_runner,
        probe_video_dims=False,
        download_timeout_s=33.0,
    )
    await p.fetch("https://www.instagram.com/p/TO2/", tmp_path)

    assert seen_timeouts == [33.0]
