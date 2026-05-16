from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from yoink.handlers import register_routers
from yoink.middleware import LoggingMiddleware

if TYPE_CHECKING:
    from yoink.config import Settings


def build_bot(settings: Settings) -> tuple[Bot, Dispatcher]:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dp = Dispatcher()

    dp.update.outer_middleware(LoggingMiddleware())

    register_routers(dp)
    return bot, dp
