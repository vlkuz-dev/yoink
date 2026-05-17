from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Dispatcher, F, Router
from aiogram.types import Message

from yoink.admin.commands import build_admin_router
from yoink.log import get_logger

if TYPE_CHECKING:
    from yoink.core.pipeline import Pipeline


def build_router(chat_allowlist: frozenset[int] = frozenset()) -> Router:
    router = Router(name="yoink.messages")
    router.message.filter(F.chat.id.in_(chat_allowlist))
    log = get_logger(__name__)

    @router.message(F.text | F.caption)
    async def handle_media_message(message: Message, pipeline: Pipeline) -> None:
        try:
            await pipeline.submit(message)
        except Exception:
            log.exception("submit_failed", chat_id=getattr(message.chat, "id", None))

    return router


def register_routers(
    dp: Dispatcher,
    *,
    admin_ids: frozenset[int] | None = None,
    chat_allowlist: frozenset[int] = frozenset(),
) -> None:
    """Register admin router first, then chat-allowlist-gated message router."""
    dp.include_router(build_admin_router(admin_ids))
    dp.include_router(build_router(chat_allowlist))
