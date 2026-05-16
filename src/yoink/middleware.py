from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

import structlog
from aiogram import BaseMiddleware

from yoink.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject

    from yoink.core.rate_limiter import TokenBucketLimiter


Handler = "Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]"


def _chat_id_from(event: Any) -> int | None:
    chat = getattr(event, "chat", None)
    if chat is None:
        return None
    cid = getattr(chat, "id", None)
    if isinstance(cid, int):
        return cid
    return None


class LoggingMiddleware(BaseMiddleware):
    """Generate a correlation_id and bind it to log context + handler data."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        correlation_id = secrets.token_hex(8)
        data["correlation_id"] = correlation_id
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        try:
            return await handler(event, data)
        finally:
            structlog.contextvars.unbind_contextvars("correlation_id")


class RateLimitMiddleware(BaseMiddleware):
    """Drop events silently when the per-chat token bucket is exhausted.

    The limiter is looked up from ``data["rate_limiter"]`` (populated via
    ``Dispatcher`` workflow data) so tests can inject a mock without touching
    bot construction.
    """

    def __init__(self, limiter: TokenBucketLimiter | None = None) -> None:
        self._log = get_logger(__name__)
        self._limiter = limiter

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        limiter = self._limiter if self._limiter is not None else data.get("rate_limiter")
        if limiter is None:
            return await handler(event, data)
        chat_id = _chat_id_from(event)
        if chat_id is None:
            return await handler(event, data)
        if not limiter.try_acquire(chat_id):
            self._log.info("rate_limited", chat_id=chat_id)
            return None
        return await handler(event, data)
