from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from yoink.downloader.runner import SubprocessResult, SubprocessTimeoutError
from yoink.providers.base import Provider
from yoink.providers.tiktok import TikTokProvider, _ToolFailed
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
