from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.types import Chat, Message, Update

from yoink.handlers import build_router
from yoink.middleware import LoggingMiddleware, RateLimitMiddleware

_CHAT_ID = 555


def _make_message(text: str = "hello", *, message_id: int = 1) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=_CHAT_ID, type="private"),
        text=text,
    )


def _make_update(message: Message, *, update_id: int = 1) -> Update:
    return Update(update_id=update_id, message=message)


class _AllowAll:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def try_acquire(self, chat_id: int) -> bool:
        self.calls.append(chat_id)
        return True


class _DenyAll:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def try_acquire(self, chat_id: int) -> bool:
        self.calls.append(chat_id)
        return False


async def _propagate(dp: Dispatcher, update: Update) -> Any:
    bot = MagicMock()
    bot.id = 1
    return await dp.feed_update(bot, update)


@pytest.mark.asyncio
async def test_handler_calls_pipeline_submit_once() -> None:
    pipeline = MagicMock()
    pipeline.submit = AsyncMock(return_value=None)

    dp = Dispatcher()
    dp.include_router(build_router())
    dp["pipeline"] = pipeline
    dp["rate_limiter"] = _AllowAll()

    msg = _make_message("https://instagram.com/p/abc")
    await _propagate(dp, _make_update(msg))

    pipeline.submit.assert_awaited_once()
    args, _ = pipeline.submit.call_args
    assert args[0].text == msg.text


@pytest.mark.asyncio
async def test_rate_limit_middleware_blocks_handler_silently() -> None:
    pipeline = MagicMock()
    pipeline.submit = AsyncMock(return_value=None)

    limiter = _DenyAll()
    dp = Dispatcher()
    dp.message.middleware(RateLimitMiddleware(limiter))
    dp.include_router(build_router())
    dp["pipeline"] = pipeline

    msg = _make_message("https://instagram.com/p/xyz")
    await _propagate(dp, _make_update(msg))

    pipeline.submit.assert_not_awaited()
    assert limiter.calls == [_CHAT_ID]


@pytest.mark.asyncio
async def test_rate_limit_middleware_reads_limiter_from_data() -> None:
    pipeline = MagicMock()
    pipeline.submit = AsyncMock(return_value=None)

    limiter = _DenyAll()
    dp = Dispatcher()
    dp.message.middleware(RateLimitMiddleware())
    dp.include_router(build_router())
    dp["pipeline"] = pipeline
    dp["rate_limiter"] = limiter

    msg = _make_message("https://instagram.com/reel/q")
    await _propagate(dp, _make_update(msg))

    pipeline.submit.assert_not_awaited()
    assert limiter.calls == [_CHAT_ID]


@pytest.mark.asyncio
async def test_logging_middleware_attaches_correlation_id() -> None:
    captured: dict[str, Any] = {}

    async def fake_handler(event: Any, data: dict[str, Any]) -> None:
        captured.update(data)

    mw = LoggingMiddleware()
    msg = _make_message()
    await mw(fake_handler, msg, {})

    assert "correlation_id" in captured
    assert isinstance(captured["correlation_id"], str)
    assert len(captured["correlation_id"]) >= 8


@pytest.mark.asyncio
async def test_rate_limit_middleware_passes_through_when_no_limiter() -> None:
    called: list[bool] = []

    async def fake_handler(event: Any, data: dict[str, Any]) -> str:
        called.append(True)
        return "ok"

    mw = RateLimitMiddleware()
    msg = _make_message()
    result = await mw(fake_handler, msg, {})

    assert called == [True]
    assert result == "ok"


@pytest.mark.asyncio
async def test_rate_limit_middleware_skips_events_without_chat() -> None:
    called: list[bool] = []

    async def fake_handler(event: Any, data: dict[str, Any]) -> str:
        called.append(True)
        return "ok"

    limiter = _DenyAll()
    mw = RateLimitMiddleware(limiter)
    result = await mw(fake_handler, object(), {})

    assert called == [True]
    assert result == "ok"
    assert limiter.calls == []
