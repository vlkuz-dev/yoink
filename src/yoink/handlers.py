from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import Message

from yoink.log import get_logger

if TYPE_CHECKING:
    from yoink.core.pipeline import Pipeline


def build_router() -> Router:
    router = Router(name="yoink.messages")
    log = get_logger(__name__)

    @router.message(F.text | F.caption)
    async def handle_media_message(message: Message, pipeline: Pipeline) -> None:
        try:
            await pipeline.submit(message)
        except Exception:
            log.exception("submit_failed", chat_id=getattr(message.chat, "id", None))

    return router
