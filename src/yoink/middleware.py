from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

import structlog
from aiogram import BaseMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import TelegramObject


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
