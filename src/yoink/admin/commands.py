from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command

from yoink.log import get_logger

if TYPE_CHECKING:
    from aiogram.types import Message

    from yoink.cache.store import FileIdCache
    from yoink.config import Settings
    from yoink.core.pipeline import Pipeline
    from yoink.providers.cookie_health import CookieHealth


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


def _human_ago(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _fmt_ts_line(label: str, ts: float | None, *, now: float | None = None) -> str:
    if ts is None:
        return f"{label}: (never)"
    when = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    delta = (now if now is not None else time.time()) - ts
    if delta < 0:
        delta = 0
    return f"{label}: {when} ({_human_ago(delta)})"


async def cmd_ig_status(
    message: Message,
    settings: Settings,
    cookie_health: CookieHealth,
) -> None:
    if not is_admin(_from_user_id(message), settings):
        return
    stat = cookie_health.stat()
    state = cookie_health.state(stat)
    lines: list[str] = ["IG cookies status"]
    lines.append(f"path: {stat.path}" if stat.path is not None else "path: (not configured)")
    if stat.path is not None:
        lines.append(f"exists: {'yes' if stat.exists else 'no'}")
        if stat.exists:
            lines.append(f"size: {stat.size_bytes} bytes")
            lines.append(_fmt_ts_line("mtime", stat.mtime))
    lines.append(_fmt_ts_line("last_success", cookie_health.last_success))
    lf = cookie_health.last_failure
    if lf is not None:
        ts, reason = lf
        lines.append(f"{_fmt_ts_line('last_failure', ts)} — {reason}")
    else:
        lines.append("last_failure: (none)")
    lines.append(f"state: {state}")
    await message.reply("\n".join(lines))


def build_admin_router(admin_ids: frozenset[int] | None = None) -> Router:
    router = Router(name="yoink.admin")
    if admin_ids is not None:
        # Filter at router level so non-admin messages skip admin handlers
        # entirely and fall through to the message router. Without this,
        # aiogram's Command filter consumes `/stats <url>` from non-admins
        # and the URL is never extracted.
        router.message.filter(F.from_user.id.in_(admin_ids))
    router.message.register(cmd_ping, Command("ping"))
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(cmd_flush, Command("flush_cache"))
    router.message.register(cmd_ig_status, Command("ig_status"))
    return router
