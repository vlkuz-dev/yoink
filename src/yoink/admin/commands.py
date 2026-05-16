from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command

from yoink.log import get_logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from yoink.cache.store import FileIdCache
    from yoink.config import Settings
    from yoink.core.pipeline import Pipeline


_log = get_logger(__name__)


def is_admin(user_id: int | None, settings: Settings) -> bool:
    if user_id is None:
        return False
    return user_id in settings.admin_ids


def _from_user_id(message: Message) -> int | None:
    user = getattr(message, "from_user", None)
    if user is None:
        return None
    uid = getattr(user, "id", None)
    return uid if isinstance(uid, int) else None


async def cmd_ping(message: Message, settings: Settings) -> None:
    if not is_admin(_from_user_id(message), settings):
        return
    await message.reply("pong")


async def cmd_stats(
    message: Message,
    settings: Settings,
    pipeline: Pipeline,
    cache: FileIdCache,
) -> None:
    if not is_admin(_from_user_id(message), settings):
        return
    stats = await cache.stats()
    text = (
        f"urls: {stats.url_count}\n"
        f"files: {stats.file_count}\n"
        f"queue: {pipeline.queue_depth}\n"
        f"workers: {pipeline.worker_count}"
    )
    await message.reply(text)


async def cmd_flush(
    message: Message,
    settings: Settings,
    cache: FileIdCache,
) -> None:
    if not is_admin(_from_user_id(message), settings):
        return
    removed = await cache.flush()
    await message.reply(f"flushed: {removed}")


def build_admin_router() -> Router:
    router = Router(name="yoink.admin")
    router.message.register(cmd_ping, Command("ping"))
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(cmd_flush, Command("flush_cache"))
    return router
