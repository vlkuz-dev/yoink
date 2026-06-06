from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from yoink.downloader.runner import SubprocessResult, SubprocessTimeoutError
from yoink.providers.base import Provider
from yoink.providers.tiktok import MediaTooLarge, TikTokProvider, _ToolFailed
from yoink.providers.tiktok import provider as module_provider

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
        self.timeouts: list[float] = []
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
        self.timeouts.append(timeout_s)
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


# --- Task 2: yt-dlp command builder (primary tool) + security flags ---


@pytest.mark.asyncio
async def test_yt_dlp_builds_secure_argv(tmp_path: Path) -> None:
    """yt-dlp argv carries the security flags; URL is the last arg after `--`."""
    url = "https://www.tiktok.com/@user/video/123"
    # rc=0 with no files written → collector returns nothing → _ToolFailed.
    # The cmd is recorded before that, so we can still inspect the argv.
    rec = _Recorder([_result()])
    p = TikTokProvider(runner=rec)

    with pytest.raises(_ToolFailed):
        await p._run_yt_dlp(url, tmp_path)

    cmd = rec.calls[0]
    assert cmd[0] == "yt-dlp"
    assert "--ignore-config" in cmd
    assert "--write-info-json" in cmd
    assert "--no-progress" in cmd
    # `--` argument-injection guard must precede the URL, which is last.
    assert cmd[-2] == "--"
    assert cmd[-1] == url


@pytest.mark.asyncio
async def test_yt_dlp_output_template_targets_workdir(tmp_path: Path) -> None:
    url = "https://www.tiktok.com/@user/video/123"
    rec = _Recorder([_result()])
    p = TikTokProvider(runner=rec)

    with pytest.raises(_ToolFailed):
        await p._run_yt_dlp(url, tmp_path)

    cmd = rec.calls[0]
    o_index = cmd.index("-o")
    assert cmd[o_index + 1] == f"{tmp_path}/%(id)s.%(ext)s"


@pytest.mark.asyncio
async def test_yt_dlp_timeout_surfaced_as_transient(tmp_path: Path) -> None:
    """A SubprocessTimeoutError is caught and re-raised as a transient _ToolFailed."""
    url = "https://www.tiktok.com/@user/video/123"
    rec = _Recorder([SubprocessTimeoutError(["yt-dlp"], 30.0)])
    p = TikTokProvider(runner=rec)

    with pytest.raises(_ToolFailed) as ei:
        await p._run_yt_dlp(url, tmp_path)

    assert ei.value.transient is True
    # The raw timeout error must not leak out uncaught.
    assert not isinstance(ei.value, SubprocessTimeoutError)


@pytest.mark.asyncio
async def test_yt_dlp_rc_nonzero_rate_limit_is_transient(tmp_path: Path) -> None:
    url = "https://www.tiktok.com/@user/video/123"
    rec = _Recorder([_result(returncode=1, stderr="HTTP Error 429: Too Many Requests")])
    p = TikTokProvider(runner=rec)

    with pytest.raises(_ToolFailed) as ei:
        await p._run_yt_dlp(url, tmp_path)

    assert ei.value.transient is True


@pytest.mark.asyncio
async def test_yt_dlp_rc_nonzero_generic_is_permanent(tmp_path: Path) -> None:
    url = "https://www.tiktok.com/@user/video/123"
    rec = _Recorder([_result(returncode=1, stderr="some unrecognized error")])
    p = TikTokProvider(runner=rec)

    with pytest.raises(_ToolFailed) as ei:
        await p._run_yt_dlp(url, tmp_path)

    assert ei.value.transient is False


# --- Task 3: artifact collection, ordering, sidecars, size cap ---


async def test_collect_single_video_probed_dims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lone .mp4 → one `video` item; missing dims are filled in by ffprobe."""
    monkeypatch.setattr(
        "yoink.providers.tiktok.shutil.which",
        lambda _name: "/usr/bin/ffprobe",
    )
    vid = tmp_path / "7123456789.mp4"
    _make_file(vid)
    # No sidecar → collector falls back to ffprobe for width/height/duration.
    ffprobe_json = json.dumps(
        {"streams": [{"width": 1080, "height": 1920, "duration": "12.5"}]},
    ).encode()
    rec = _Recorder([_result(stdout=ffprobe_json)])
    p = TikTokProvider(runner=rec)  # probe_video_dims defaults True

    items = await p._collect_items(tmp_path)

    assert len(items) == 1
    it = items[0]
    assert it.kind == "video"
    assert it.mime == "video/mp4"
    assert it.width == 1080
    assert it.height == 1920
    assert it.duration_s == 12  # int(float("12.5"))
    # ffprobe was actually invoked.
    assert rec.calls[0][0] == "ffprobe"


async def test_collect_single_photo(tmp_path: Path) -> None:
    """A single slideshow image → one `photo` item with sidecar dims."""
    img = tmp_path / "7123456789_1.jpg"
    _make_file(img)
    _write_sidecar(img, {"width": 1080, "height": 1080})
    p = TikTokProvider(probe_video_dims=False)

    items = await p._collect_items(tmp_path)

    assert len(items) == 1
    it = items[0]
    assert it.kind == "photo"
    assert it.mime == "image/jpeg"
    assert it.width == 1080
    assert it.height == 1080
    assert it.path.name == "7123456789_1.jpg"


async def test_collect_slideshow_numeric_order(tmp_path: Path) -> None:
    """10 slide images must keep numeric order (`_2` before `_10`)."""
    for i in range(1, 11):
        img = tmp_path / f"slides_{i}.jpg"
        _make_file(img)
        _write_sidecar(img, {"width": 1080, "height": 1350})
    p = TikTokProvider(probe_video_dims=False)

    items = await p._collect_items(tmp_path)

    names = [it.path.name for it in items]
    assert names == [f"slides_{i}.jpg" for i in range(1, 11)]
    assert all(it.kind == "photo" for it in items)


async def test_collect_skips_sidecars_and_dotfiles(tmp_path: Path) -> None:
    """`.json` / `.info.json` sidecars, dotfiles and non-media are ignored."""
    img = tmp_path / "p1.jpg"
    _make_file(img)
    _write_sidecar(img, {"width": 800, "height": 600})
    # yt-dlp-style info.json sidecar, a stray dotfile, and a non-media file.
    (tmp_path / "p1.info.json").write_text(
        json.dumps({"width": 800, "height": 600}),
        encoding="utf-8",
    )
    (tmp_path / ".cache").write_bytes(b"meta")
    (tmp_path / "notes.txt").write_bytes(b"hi")
    p = TikTokProvider(probe_video_dims=False)

    items = await p._collect_items(tmp_path)

    assert len(items) == 1
    assert items[0].path.name == "p1.jpg"


def test_enforce_size_raises_media_too_large(tmp_path: Path) -> None:
    big = tmp_path / "big.mp4"
    _make_file(big, size=4096)
    p = TikTokProvider(probe_video_dims=False, max_file_bytes=128)

    with pytest.raises(MediaTooLarge) as ei:
        p._enforce_size(big)
    assert ei.value.size_bytes == 4096
    assert ei.value.limit_bytes == 128


def test_enforce_size_noop_when_no_limit(tmp_path: Path) -> None:
    big = tmp_path / "ok.mp4"
    _make_file(big, size=4096)
    p = TikTokProvider(probe_video_dims=False)  # no max_file_bytes, no env

    # No limit configured → never raises.
    p._enforce_size(big)


def test_enforce_size_uses_env_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOINK_MAX_FILE_MB", "1")
    small = tmp_path / "s.mp4"
    _make_file(small, size=8)
    big = tmp_path / "b.mp4"
    _make_file(big, size=2 * 1024 * 1024)
    p = TikTokProvider(probe_video_dims=False)  # falls back to env

    p._enforce_size(small)  # 8 bytes < 1 MiB → ok
    with pytest.raises(MediaTooLarge):
        p._enforce_size(big)  # 2 MiB > 1 MiB


def test_purge_workdir_removes_stale_preserves_heartbeat(tmp_path: Path) -> None:
    stale_file = tmp_path / "stale.mp4"
    _make_file(stale_file, size=4)
    stale_dir = tmp_path / "subdir"
    _make_file(stale_dir / "nested.jpg", size=4)
    heartbeat = tmp_path / ".heartbeat"
    _make_file(heartbeat, size=1)

    TikTokProvider._purge_workdir(tmp_path)

    assert not stale_file.exists()
    assert not stale_dir.exists()
    # The pipeline heartbeat sentinel must survive a purge.
    assert heartbeat.exists()


def test_purge_workdir_missing_dir_is_noop(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    # Must not raise when the workdir was never created.
    TikTokProvider._purge_workdir(missing)


# --- Task 4: fetch() orchestration (yt-dlp primary, happy path) ---


def _yt_dlp_writes(files: list[tuple[str, dict[str, object] | None]]) -> SideEffect:
    """Build a side_effect that synthesises yt-dlp output into the workdir.

    Each entry is `(relative_name, sidecar_meta_or_None)`. ffprobe calls
    (cmd[0] == "ffprobe") are ignored so video probing still works.
    """

    def _effect(cmd: list[str], cwd: Path) -> None:
        if cmd and cmd[0] == "ffprobe":
            return
        for name, meta in files:
            target = cwd / name
            _make_file(target)
            if meta is not None:
                _write_sidecar(target, meta)

    return _effect


@pytest.mark.asyncio
async def test_fetch_single_video_happy_path(tmp_path: Path) -> None:
    url = "https://www.tiktok.com/@user/video/7123456789"
    rec = _Recorder(
        [_result()],
        side_effect=_yt_dlp_writes(
            [("7123456789.mp4", {"width": 1080, "height": 1920, "duration": 12})],
        ),
    )
    p = TikTokProvider(runner=rec, probe_video_dims=False)

    pkg = await p.fetch(url, tmp_path)

    assert pkg.source_url == url
    assert pkg.provider == "tiktok"
    assert pkg.caption is None
    assert len(pkg.items) == 1
    it = pkg.items[0]
    assert it.kind == "video"
    assert it.path.name == "7123456789.mp4"
    assert it.width == 1080
    assert it.height == 1920
    # Only the yt-dlp invocation ran (no fallback on the happy path).
    assert rec.calls[0][0] == "yt-dlp"
    assert len(rec.calls) == 1


@pytest.mark.asyncio
async def test_fetch_slideshow_three_photos_stable_order(tmp_path: Path) -> None:
    url = "https://www.tiktok.com/@user/photo/7123456789"
    rec = _Recorder(
        [_result()],
        side_effect=_yt_dlp_writes(
            [
                (f"7123456789_{i}.jpg", {"width": 1080, "height": 1350})
                for i in range(1, 4)
            ],
        ),
    )
    p = TikTokProvider(runner=rec, probe_video_dims=False)

    pkg = await p.fetch(url, tmp_path)

    names = [it.path.name for it in pkg.items]
    assert names == [f"7123456789_{i}.jpg" for i in range(1, 4)]
    assert all(it.kind == "photo" for it in pkg.items)


@pytest.mark.asyncio
async def test_fetch_slideshow_ten_photos_numeric_order(tmp_path: Path) -> None:
    url = "https://www.tiktok.com/@user/photo/7123456789"
    rec = _Recorder(
        [_result()],
        side_effect=_yt_dlp_writes(
            [
                (f"slide_{i}.jpg", {"width": 1080, "height": 1350})
                for i in range(1, 11)
            ],
        ),
    )
    p = TikTokProvider(runner=rec, probe_video_dims=False)

    pkg = await p.fetch(url, tmp_path)

    names = [it.path.name for it in pkg.items]
    # `_2` must precede `_10` (numeric, not lexicographic, ordering).
    assert names == [f"slide_{i}.jpg" for i in range(1, 11)]


@pytest.mark.asyncio
async def test_fetch_purges_stale_before_tool(tmp_path: Path) -> None:
    url = "https://www.tiktok.com/@user/video/7123456789"
    # A leftover file from a prior failed attempt must be purged so it is
    # not returned as a successful artifact.
    stale = tmp_path / "stale_old.mp4"
    _make_file(stale, size=4)
    rec = _Recorder(
        [_result()],
        side_effect=_yt_dlp_writes(
            [("7123456789.mp4", {"width": 720, "height": 1280, "duration": 5})],
        ),
    )
    p = TikTokProvider(runner=rec, probe_video_dims=False)

    pkg = await p.fetch(url, tmp_path)

    names = [it.path.name for it in pkg.items]
    assert "stale_old.mp4" not in names
    assert names == ["7123456789.mp4"]


@pytest.mark.asyncio
async def test_fetch_too_large_raises(tmp_path: Path) -> None:
    url = "https://www.tiktok.com/@user/video/7123456789"
    rec = _Recorder(
        [_result()],
        side_effect=_yt_dlp_writes(
            [("7123456789.mp4", {"width": 720, "height": 1280, "duration": 5})],
        ),
    )
    p = TikTokProvider(runner=rec, probe_video_dims=False, max_file_bytes=4)

    with pytest.raises(MediaTooLarge):
        await p.fetch(url, tmp_path)


# --- Task 5: gallery-dl fallback + error classification ---


def _tool_writes(
    by_tool: dict[str, list[tuple[str, dict[str, object] | None]]],
) -> SideEffect:
    """Build a side_effect that synthesises output per-tool into the workdir.

    Keyed on `cmd[0]` (e.g. ``"yt-dlp"`` / ``"gallery-dl"``) so a test can
    make one extractor write files and the other write nothing. ffprobe
    calls are ignored so video probing still works.
    """

    def _effect(cmd: list[str], cwd: Path) -> None:
        if not cmd or cmd[0] == "ffprobe":
            return
        for name, meta in by_tool.get(cmd[0], []):
            target = cwd / name
            _make_file(target)
            if meta is not None:
                _write_sidecar(target, meta)

    return _effect


@pytest.mark.asyncio
async def test_gallery_dl_builds_secure_argv(tmp_path: Path) -> None:
    """gallery-dl argv carries the security flags; URL is the last arg after `--`."""
    url = "https://www.tiktok.com/@user/video/123"
    # rc=0 with no files written → collector returns nothing → _ToolFailed.
    rec = _Recorder([_result()])
    p = TikTokProvider(runner=rec)

    with pytest.raises(_ToolFailed):
        await p._run_gallery_dl(url, tmp_path)

    cmd = rec.calls[0]
    assert cmd[0] == "gallery-dl"
    assert "--config-ignore" in cmd
    assert "--write-metadata" in cmd
    assert "--no-part" in cmd
    d_index = cmd.index("-D")
    assert cmd[d_index + 1] == str(tmp_path)
    # `--` argument-injection guard must precede the URL, which is last.
    assert cmd[-2] == "--"
    assert cmd[-1] == url


@pytest.mark.asyncio
async def test_gallery_dl_timeout_surfaced_as_transient(tmp_path: Path) -> None:
    url = "https://www.tiktok.com/@user/video/123"
    rec = _Recorder([SubprocessTimeoutError(["gallery-dl"], 30.0)])
    p = TikTokProvider(runner=rec)

    with pytest.raises(_ToolFailed) as ei:
        await p._run_gallery_dl(url, tmp_path)

    assert ei.value.transient is True
    assert not isinstance(ei.value, SubprocessTimeoutError)


@pytest.mark.asyncio
async def test_fetch_yt_dlp_rc_nonzero_falls_back_to_gallery_dl(
    tmp_path: Path,
) -> None:
    """yt-dlp rc!=0 → gallery-dl runs and its result is returned."""
    url = "https://www.tiktok.com/@user/video/7123456789"
    rec = _Recorder(
        [
            _result(returncode=1, stderr="some unrecognized yt-dlp error"),
            _result(),  # gallery-dl succeeds
        ],
        side_effect=_tool_writes(
            {"gallery-dl": [("7123456789.jpg", {"width": 1080, "height": 1080})]},
        ),
    )
    p = TikTokProvider(runner=rec, probe_video_dims=False)

    pkg = await p.fetch(url, tmp_path)

    assert len(pkg.items) == 1
    assert pkg.items[0].path.name == "7123456789.jpg"
    # Both extractors were invoked, in order.
    assert [c[0] for c in rec.calls] == ["yt-dlp", "gallery-dl"]


@pytest.mark.asyncio
async def test_fetch_yt_dlp_zero_items_falls_back_to_gallery_dl(
    tmp_path: Path,
) -> None:
    """yt-dlp exits 0 but writes nothing → gallery-dl fallback runs."""
    url = "https://www.tiktok.com/@user/photo/7123456789"
    rec = _Recorder(
        [
            _result(),  # yt-dlp rc=0 but writes no files
            _result(),  # gallery-dl succeeds
        ],
        side_effect=_tool_writes(
            {
                "gallery-dl": [
                    (f"7123456789_{i}.jpg", {"width": 1080, "height": 1350})
                    for i in range(1, 4)
                ],
            },
        ),
    )
    p = TikTokProvider(runner=rec, probe_video_dims=False)

    pkg = await p.fetch(url, tmp_path)

    names = [it.path.name for it in pkg.items]
    assert names == [f"7123456789_{i}.jpg" for i in range(1, 4)]
    assert [c[0] for c in rec.calls] == ["yt-dlp", "gallery-dl"]


@pytest.mark.asyncio
async def test_fetch_both_extractors_permanent_raises_provider_error(
    tmp_path: Path,
) -> None:
    from yoink.providers.tiktok import ProviderError

    url = "https://www.tiktok.com/@user/video/7123456789"
    rec = _Recorder(
        [
            _result(returncode=1, stderr="some unrecognized error"),
            _result(returncode=1, stderr="another unrecognized error"),
        ],
    )
    p = TikTokProvider(runner=rec, probe_video_dims=False)

    with pytest.raises(ProviderError) as ei:
        await p.fetch(url, tmp_path)
    assert ei.value.url == url
    assert [c[0] for c in rec.calls] == ["yt-dlp", "gallery-dl"]


@pytest.mark.asyncio
async def test_fetch_primary_transient_then_permanent_is_transient(
    tmp_path: Path,
) -> None:
    """yt-dlp transient + gallery-dl permanent → combined result is transient."""
    from yoink.providers.tiktok import ProviderTransientError

    url = "https://www.tiktok.com/@user/video/7123456789"
    rec = _Recorder(
        [
            _result(returncode=1, stderr="HTTP Error 429: Too Many Requests"),
            _result(returncode=1, stderr="some unrecognized error"),
        ],
    )
    p = TikTokProvider(runner=rec, probe_video_dims=False)

    with pytest.raises(ProviderTransientError):
        await p.fetch(url, tmp_path)


@pytest.mark.asyncio
async def test_fetch_primary_permanent_then_transient_is_transient(
    tmp_path: Path,
) -> None:
    """yt-dlp permanent + gallery-dl 5xx → combined result is transient."""
    from yoink.providers.tiktok import ProviderTransientError

    url = "https://www.tiktok.com/@user/video/7123456789"
    rec = _Recorder(
        [
            _result(returncode=1, stderr="some unrecognized error"),
            _result(returncode=1, stderr="HTTP Error 503: Service Unavailable"),
        ],
    )
    p = TikTokProvider(runner=rec, probe_video_dims=False)

    with pytest.raises(ProviderTransientError):
        await p.fetch(url, tmp_path)


# --- Task 6: configure() + env-var fallback ---


def test_configure_applies_max_bytes_and_timeout() -> None:
    p = TikTokProvider(probe_video_dims=False)
    # Defaults before configure: no max-bytes limit, default timeout.
    assert p._effective_max_bytes() is None
    assert p._effective_timeout() == 90.0

    p.configure(max_file_bytes=42 * 1024 * 1024, download_timeout_s=120.0)

    assert p._effective_max_bytes() == 42 * 1024 * 1024
    assert p._effective_timeout() == 120.0


def test_configure_no_cookies_signature() -> None:
    # TikTok needs no cookies — configure must reject cookie kwargs.
    p = TikTokProvider(probe_video_dims=False)
    with pytest.raises(TypeError):
        p.configure(cookies_file=Path("/tmp/c.txt"))  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_no_cookie_flags_in_either_tool_argv(tmp_path: Path) -> None:
    # The behavioral "public posts only" contract: neither extractor argv may
    # ever carry a cookie flag (the signature guard above only proves the
    # kwarg is absent, not that the argv stays clean).
    url = "https://www.tiktok.com/@user/video/123"
    cookie_flags = ("--cookies", "--cookies-from-browser")
    for builder in ("_run_yt_dlp", "_run_gallery_dl"):
        # rc=0 with no files written → _ToolFailed, but argv is recorded first.
        rec = _Recorder([_result()])
        p = TikTokProvider(runner=rec)
        with pytest.raises(_ToolFailed):
            await getattr(p, builder)(url, tmp_path)
        cmd = rec.calls[0]
        assert not any(flag in arg for arg in cmd for flag in cookie_flags), (
            f"{builder} argv leaked a cookie flag: {cmd}"
        )


def test_configure_partial_leaves_other_value_untouched() -> None:
    p = TikTokProvider(
        probe_video_dims=False, max_file_bytes=999, download_timeout_s=10.0
    )
    # Only update timeout; max_file_bytes must be preserved.
    p.configure(download_timeout_s=55.0)
    assert p._effective_timeout() == 55.0
    assert p._effective_max_bytes() == 999

    # None args are no-ops (do not clobber configured values).
    p.configure(max_file_bytes=None, download_timeout_s=None)
    assert p._effective_timeout() == 55.0
    assert p._effective_max_bytes() == 999


def test_timeout_env_var_fallback_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOINK_DOWNLOAD_TIMEOUT_S", "45")
    p = TikTokProvider(probe_video_dims=False)  # no explicit timeout
    assert p._effective_timeout() == 45.0


def test_timeout_env_var_invalid_falls_through_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOINK_DOWNLOAD_TIMEOUT_S", "not-a-number")
    p = TikTokProvider(probe_video_dims=False)
    assert p._effective_timeout() == 90.0


def test_explicit_timeout_overrides_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOINK_DOWNLOAD_TIMEOUT_S", "45")
    # ctor value wins over the env var.
    p = TikTokProvider(probe_video_dims=False, download_timeout_s=200.0)
    assert p._effective_timeout() == 200.0
    # configure() value also wins over the env var.
    p2 = TikTokProvider(probe_video_dims=False)
    p2.configure(download_timeout_s=300.0)
    assert p2._effective_timeout() == 300.0


def test_explicit_max_bytes_overrides_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOINK_MAX_FILE_MB", "1")
    p = TikTokProvider(probe_video_dims=False)
    p.configure(max_file_bytes=7 * 1024 * 1024)
    # Configured byte count wins over the 1 MiB env fallback.
    assert p._effective_max_bytes() == 7 * 1024 * 1024


@pytest.mark.asyncio
async def test_configured_timeout_passed_to_runner(tmp_path: Path) -> None:
    url = "https://www.tiktok.com/@user/video/7123456789"
    rec = _Recorder(
        [_result()],
        side_effect=_yt_dlp_writes(
            [("7123456789.mp4", {"width": 720, "height": 1280, "duration": 5})],
        ),
    )
    p = TikTokProvider(runner=rec, probe_video_dims=False)
    p.configure(download_timeout_s=137.0)

    await p.fetch(url, tmp_path)

    # The yt-dlp subprocess must receive the configured timeout.
    assert rec.timeouts == [137.0]


def test_module_singleton_is_configurable() -> None:
    # The autodiscovered module-level singleton exposes configure (mirrors
    # how __main__ wires it before ProviderRegistry.autodiscover()).
    assert hasattr(module_provider, "configure")
    saved_max = module_provider._max_file_bytes
    saved_timeout = module_provider._download_timeout_s
    try:
        module_provider.configure(
            max_file_bytes=5 * 1024 * 1024, download_timeout_s=33.0
        )
        assert module_provider._effective_max_bytes() == 5 * 1024 * 1024
        assert module_provider._effective_timeout() == 33.0
    finally:
        module_provider._max_file_bytes = saved_max
        module_provider._download_timeout_s = saved_timeout
