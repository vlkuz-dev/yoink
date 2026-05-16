from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from yoink.admin.commands import cmd_flush, cmd_ping, cmd_stats, is_admin
from yoink.cache.store import CachedFile, FileIdCache
from yoink.config import Settings
from yoink.core.pipeline import Pipeline
from yoink.core.registry import ProviderRegistry

_CHAT_ID = 999
_ADMIN_UID = 42
_NON_ADMIN_UID = 7


def _make_settings(*, admin_ids: frozenset[int] = frozenset({_ADMIN_UID})) -> Settings:
    return Settings(
        bot_token="x" * 8,
        admin_ids=admin_ids,
        cache_db=Path("/tmp/yoink-test-cache.sqlite"),
        workdir=Path("/tmp/yoink-test-workdir"),
    )


def _make_msg(user_id: int) -> Any:
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=_CHAT_ID),
        reply=AsyncMock(return_value=None),
    )
    return msg


def test_is_admin_true_when_in_set() -> None:
    s = _make_settings(admin_ids=frozenset({1, 2, 3}))
    assert is_admin(2, s) is True


def test_is_admin_false_when_missing() -> None:
    s = _make_settings(admin_ids=frozenset({1, 2}))
    assert is_admin(99, s) is False


def test_is_admin_none_user_returns_false() -> None:
    s = _make_settings(admin_ids=frozenset({1}))
    assert is_admin(None, s) is False


@pytest.mark.asyncio
async def test_ping_admin_replies_pong() -> None:
    settings = _make_settings()
    msg = _make_msg(_ADMIN_UID)
    await cmd_ping(msg, settings)
    msg.reply.assert_awaited_once_with("pong")


@pytest.mark.asyncio
async def test_ping_non_admin_silent() -> None:
    settings = _make_settings()
    msg = _make_msg(_NON_ADMIN_UID)
    await cmd_ping(msg, settings)
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_stats_admin_reports_counts(tmp_path: Path) -> None:
    settings = _make_settings()
    cache = FileIdCache(tmp_path / "c.sqlite")
    await cache.init()
    try:
        await cache.put(
            "h1",
            "https://www.instagram.com/p/abc/",
            "instagram",
            [CachedFile(file_id="A", kind="photo", mime="image/jpeg")],
        )
        await cache.put(
            "h2",
            "https://www.instagram.com/p/def/",
            "instagram",
            [
                CachedFile(file_id="B", kind="photo", mime="image/jpeg"),
                CachedFile(file_id="C", kind="video", mime="video/mp4"),
            ],
        )

        pipeline = SimpleNamespace(queue_depth=4, worker_count=2)
        msg = _make_msg(_ADMIN_UID)
        await cmd_stats(msg, settings, pipeline, cache)

        msg.reply.assert_awaited_once()
        text = msg.reply.await_args.args[0]
        assert "urls: 2" in text
        assert "files: 3" in text
        assert "queue: 4" in text
        assert "workers: 2" in text
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_stats_non_admin_silent(tmp_path: Path) -> None:
    settings = _make_settings()
    cache = FileIdCache(tmp_path / "c.sqlite")
    await cache.init()
    try:
        pipeline = SimpleNamespace(queue_depth=0, worker_count=1)
        msg = _make_msg(_NON_ADMIN_UID)
        await cmd_stats(msg, settings, pipeline, cache)
        msg.reply.assert_not_awaited()
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_flush_admin_clears_cache(tmp_path: Path) -> None:
    settings = _make_settings()
    cache = FileIdCache(tmp_path / "c.sqlite")
    await cache.init()
    try:
        await cache.put(
            "h1",
            "https://www.instagram.com/p/abc/",
            "instagram",
            [CachedFile(file_id="A", kind="photo", mime="image/jpeg")],
        )

        msg = _make_msg(_ADMIN_UID)
        await cmd_flush(msg, settings, cache)

        msg.reply.assert_awaited_once()
        text = msg.reply.await_args.args[0]
        assert "flushed: 1" in text

        stats = await cache.stats()
        assert stats.url_count == 0
        assert stats.file_count == 0
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_flush_non_admin_silent(tmp_path: Path) -> None:
    settings = _make_settings()
    cache = FileIdCache(tmp_path / "c.sqlite")
    await cache.init()
    try:
        await cache.put(
            "h1",
            "https://www.instagram.com/p/abc/",
            "instagram",
            [CachedFile(file_id="A", kind="photo", mime="image/jpeg")],
        )

        msg = _make_msg(_NON_ADMIN_UID)
        await cmd_flush(msg, settings, cache)

        msg.reply.assert_not_awaited()
        stats = await cache.stats()
        assert stats.url_count == 1
    finally:
        await cache.close()


def test_admin_router_registers_three_commands() -> None:
    from yoink.admin.commands import build_admin_router

    router = build_admin_router()
    # router.message observer has 3 handlers registered
    assert len(router.message.handlers) == 3


def test_admin_router_applies_admin_filter_when_ids_passed() -> None:
    from yoink.admin.commands import build_admin_router

    # With admin_ids set, router-level filter is installed so non-admin
    # messages skip admin handlers entirely (and fall through to message
    # router for URL extraction).
    router = build_admin_router(frozenset({42}))
    assert len(router.message._handler.filters) >= 1


def test_admin_router_no_filter_when_admin_ids_omitted() -> None:
    from yoink.admin.commands import build_admin_router

    router = build_admin_router(None)
    assert router.message._handler.filters == []


class _AllowAll:
    def try_acquire(self, _chat_id: int) -> bool:
        return True


class _StubProvider:
    name = "instagram"
    domains = frozenset({"instagram.com", "www.instagram.com"})

    def can_handle(self, url: str) -> bool:
        return "instagram.com" in url

    async def fetch(self, url: str, workdir: Path) -> Any:
        raise AssertionError("fetch should not run in allowlist filter tests")


def _pipeline_message(url: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=url,
        caption=None,
        entities=None,
        caption_entities=None,
        chat=SimpleNamespace(id=_CHAT_ID),
        from_user=SimpleNamespace(id=1),
        message_id=10,
    )


async def _build_pipeline(
    tmp_path: Path,
    *,
    allowlist: frozenset[str] | None,
) -> tuple[Pipeline, FileIdCache]:
    cache = FileIdCache(tmp_path / "p.sqlite")
    await cache.init()
    registry = ProviderRegistry()
    registry.register(_StubProvider())
    pipeline = Pipeline(
        registry=registry,
        cache=cache,
        rate_limiter=_AllowAll(),  # type: ignore[arg-type]
        uploader=MagicMock(),  # type: ignore[arg-type]
        workdir_root=tmp_path / "work",
        workers=1,
        allowlist=allowlist,
    )
    return pipeline, cache


def _known_domains() -> frozenset[str]:
    r = ProviderRegistry()
    r.register(_StubProvider())
    return r.known_domains


@pytest.mark.asyncio
async def test_pipeline_allowlist_rejects_unknown_domain(tmp_path: Path) -> None:
    pipeline, cache = await _build_pipeline(tmp_path, allowlist=_known_domains())
    try:
        await pipeline.submit(_pipeline_message("https://evil.example/p/123/"))
        assert pipeline.queue_depth == 0
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_pipeline_allowlist_passes_known_domain(tmp_path: Path) -> None:
    pipeline, cache = await _build_pipeline(tmp_path, allowlist=_known_domains())
    try:
        await pipeline.submit(_pipeline_message("https://www.instagram.com/p/abc/"))
        assert pipeline.queue_depth == 1
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_pipeline_no_allowlist_lets_registry_decide(tmp_path: Path) -> None:
    pipeline, cache = await _build_pipeline(tmp_path, allowlist=None)
    try:
        await pipeline.submit(_pipeline_message("https://example.com/no-match"))
        assert pipeline.queue_depth == 0  # registry filtered
        await pipeline.submit(_pipeline_message("https://www.instagram.com/p/xyz/"))
        assert pipeline.queue_depth == 1
    finally:
        await cache.close()


def test_registry_known_domains_is_normalized() -> None:
    r = ProviderRegistry()
    r.register(_StubProvider())
    domains = r.known_domains
    assert "instagram.com" in domains
    # www. prefix is stripped during registration
    assert "www.instagram.com" not in domains
