from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.types import Chat, Message, MessageOriginUser, PhotoSize, Update, User, Video

from yoink.handlers import build_router
from yoink.middleware import LoggingMiddleware

_CHAT_ID = 555


def _make_message(
    text: str = "hello",
    *,
    message_id: int = 1,
    chat_id: int = _CHAT_ID,
    chat_type: str = "private",
) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=chat_type),
        text=text,
    )


def _make_update(message: Message, *, update_id: int = 1) -> Update:
    return Update(update_id=update_id, message=message)


async def _propagate(dp: Dispatcher, update: Update) -> Any:
    bot = MagicMock()
    bot.id = 1
    return await dp.feed_update(bot, update)


@pytest.mark.asyncio
async def test_handler_calls_pipeline_submit_once() -> None:
    pipeline = MagicMock()
    pipeline.submit = AsyncMock(return_value=None)

    dp = Dispatcher()
    dp.include_router(build_router(frozenset({_CHAT_ID})))
    dp["pipeline"] = pipeline

    msg = _make_message("https://instagram.com/p/abc")
    await _propagate(dp, _make_update(msg))

    pipeline.submit.assert_awaited_once()
    args, _ = pipeline.submit.call_args
    assert args[0].text == msg.text


@pytest.mark.asyncio
async def test_handler_swallows_submit_exception() -> None:
    pipeline = MagicMock()
    pipeline.submit = AsyncMock(side_effect=RuntimeError("boom"))

    dp = Dispatcher()
    dp.include_router(build_router(frozenset({_CHAT_ID})))
    dp["pipeline"] = pipeline

    msg = _make_message("https://instagram.com/p/x")
    # Should not propagate
    await _propagate(dp, _make_update(msg))
    pipeline.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_handles_message_from_allowlisted_chat() -> None:
    pipeline = MagicMock()
    pipeline.submit = AsyncMock(return_value=None)

    allowed = -1001234567890
    dp = Dispatcher()
    dp.include_router(build_router(frozenset({allowed})))
    dp["pipeline"] = pipeline

    msg = _make_message("https://instagram.com/p/x", chat_id=allowed, chat_type="supergroup")
    await _propagate(dp, _make_update(msg))

    pipeline.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_skips_message_from_non_allowlisted_chat() -> None:
    pipeline = MagicMock()
    pipeline.submit = AsyncMock(return_value=None)

    dp = Dispatcher()
    dp.include_router(build_router(frozenset({-1001234567890})))
    dp["pipeline"] = pipeline

    msg = _make_message("https://instagram.com/p/x", chat_id=-1, chat_type="supergroup")
    await _propagate(dp, _make_update(msg))

    pipeline.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_skips_forwarded_message() -> None:
    pipeline = MagicMock()
    pipeline.submit = AsyncMock(return_value=None)

    dp = Dispatcher()
    dp.include_router(build_router(frozenset({_CHAT_ID})))
    dp["pipeline"] = pipeline

    now = datetime.now(UTC)
    msg = Message(
        message_id=1,
        date=now,
        chat=Chat(id=_CHAT_ID, type="private"),
        text="https://instagram.com/p/x",
        forward_date=now,
        forward_origin=MessageOriginUser(
            type="user",
            date=now,
            sender_user=User(id=42, is_bot=False, first_name="A"),
        ),
    )
    await _propagate(dp, _make_update(msg))
    pipeline.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_skips_message_from_bot_sender() -> None:
    pipeline = MagicMock()
    pipeline.submit = AsyncMock(return_value=None)

    dp = Dispatcher()
    dp.include_router(build_router(frozenset({_CHAT_ID})))
    dp["pipeline"] = pipeline

    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=_CHAT_ID, type="private"),
        text="https://instagram.com/p/x",
        from_user=User(id=99, is_bot=True, first_name="saveasbot"),
    )
    await _propagate(dp, _make_update(msg))
    pipeline.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_skips_message_with_video() -> None:
    pipeline = MagicMock()
    pipeline.submit = AsyncMock(return_value=None)

    dp = Dispatcher()
    dp.include_router(build_router(frozenset({_CHAT_ID})))
    dp["pipeline"] = pipeline

    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=_CHAT_ID, type="private"),
        caption="https://instagram.com/p/x",
        video=Video(
            file_id="v1",
            file_unique_id="vu1",
            width=10,
            height=10,
            duration=1,
        ),
    )
    await _propagate(dp, _make_update(msg))
    pipeline.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_skips_message_with_photo() -> None:
    pipeline = MagicMock()
    pipeline.submit = AsyncMock(return_value=None)

    dp = Dispatcher()
    dp.include_router(build_router(frozenset({_CHAT_ID})))
    dp["pipeline"] = pipeline

    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=_CHAT_ID, type="private"),
        caption="https://instagram.com/p/x",
        photo=[PhotoSize(file_id="p1", file_unique_id="pu1", width=10, height=10)],
    )
    await _propagate(dp, _make_update(msg))
    pipeline.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_skips_all_when_allowlist_empty() -> None:
    pipeline = MagicMock()
    pipeline.submit = AsyncMock(return_value=None)

    dp = Dispatcher()
    dp.include_router(build_router())
    dp["pipeline"] = pipeline

    msg = _make_message("https://instagram.com/p/x", chat_id=_CHAT_ID)
    await _propagate(dp, _make_update(msg))

    pipeline.submit.assert_not_awaited()


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
async def test_logging_middleware_unbinds_on_exception() -> None:
    import structlog

    async def boom_handler(event: Any, data: dict[str, Any]) -> None:
        raise RuntimeError("kaboom")

    mw = LoggingMiddleware()
    msg = _make_message()
    with pytest.raises(RuntimeError):
        await mw(boom_handler, msg, {})

    # correlation_id should not leak across calls
    assert "correlation_id" not in structlog.contextvars.get_contextvars()
