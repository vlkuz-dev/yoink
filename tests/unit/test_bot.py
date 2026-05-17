from __future__ import annotations

import pytest
from aiogram import Bot, Dispatcher

from yoink.bot import build_bot
from yoink.config import Settings


@pytest.mark.asyncio
async def test_build_bot_returns_bot_and_dispatcher() -> None:
    settings = Settings(bot_token="123:abcDEF")
    bot, dp = build_bot(settings)
    try:
        assert isinstance(bot, Bot)
        assert isinstance(dp, Dispatcher)
    finally:
        await bot.session.close()
