from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import Message, Update

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject


def _extract_chat_id(event: object) -> int | None:
    msg: object | None
    if isinstance(event, Update):
        msg = event.message or event.edited_message
    elif isinstance(event, Message):
        msg = event
    else:
        msg = None
    chat = getattr(msg, "chat", None)
    return getattr(chat, "id", None) if chat is not None else None


class LoggingMiddleware(BaseMiddleware):
    """Generate a correlation_id and bind it (+ chat_id) to log context + handler data."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        correlation_id = secrets.token_hex(8)
        data["correlation_id"] = correlation_id
        bound: dict[str, Any] = {"correlation_id": correlation_id}
        chat_id = _extract_chat_id(event)
        if chat_id is not None:
            bound["chat_id"] = chat_id
            structlog.get_logger(__name__).info("update_received", chat_id=chat_id)
        structlog.contextvars.bind_contextvars(**bound)
        try:
            return await handler(event, data)
        finally:
            structlog.contextvars.unbind_contextvars(*bound.keys())
