from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

from yoink.admin.notifier import AdminNotifier
from yoink.bot import build_bot
from yoink.cache.store import FileIdCache
from yoink.config import Settings
from yoink.core.pipeline import Pipeline
from yoink.core.rate_limiter import TokenBucketLimiter
from yoink.core.registry import ProviderRegistry
from yoink.log import configure_logging, get_logger
from yoink.providers.cookie_health import CookieHealth
from yoink.providers.instagram import provider as instagram_provider
from yoink.providers.tiktok import provider as tiktok_provider
from yoink.uploader.telegram import TelegramUploader


async def _run() -> int:
    settings = Settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log = get_logger("yoink")
    log.info(
        "yoink starting",
        workers=settings.workers,
        queue_maxsize=settings.queue_maxsize,
        log_format=settings.log_format,
    )

    cache = FileIdCache(settings.cache_db)
    await cache.init()

    cookie_health = CookieHealth()
    instagram_provider.configure(
        cookies_file=settings.ig_cookies_file,
        max_file_bytes=settings.max_file_mb * 1024 * 1024,
        download_timeout_s=float(settings.download_timeout_s),
        cookie_health=cookie_health,
    )
    tiktok_provider.configure(
        max_file_bytes=settings.max_file_mb * 1024 * 1024,
        download_timeout_s=float(settings.download_timeout_s),
    )
    registry = ProviderRegistry.autodiscover()
    rate_limiter = TokenBucketLimiter(rate_per_min=settings.rate_per_chat_per_min)
    user_rate_limiter = TokenBucketLimiter(
        rate_per_hour=settings.rate_per_user_per_hour,
        idle_gc_seconds=2 * 3600.0,
    )
    bot, dp = build_bot(settings)
    uploader = TelegramUploader(bot)
    notifier = AdminNotifier(
        bot=bot,
        admin_ids=settings.admin_ids,
        log=get_logger("yoink.notifier"),
    )
    allowlist = registry.known_domains if settings.allowlist_mode else None
    pipeline = Pipeline(
        registry=registry,
        cache=cache,
        rate_limiter=rate_limiter,
        uploader=uploader,
        workdir_root=settings.workdir,
        workers=settings.workers,
        queue_maxsize=settings.queue_maxsize,
        allowlist=allowlist,
        notifier=notifier,
        cookie_health=cookie_health,
        user_rate_limiter=user_rate_limiter,
    )

    dp["pipeline"] = pipeline
    dp["settings"] = settings
    dp["cache"] = cache
    dp["cookie_health"] = cookie_health

    await pipeline.start()
    log.info("pipeline started")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    poll_task = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False, allowed_updates=["message"]),
        name="yoink-polling",
    )
    stop_task = asyncio.create_task(stop_event.wait(), name="yoink-stop")

    poll_exc: BaseException | None = None
    try:
        await asyncio.wait(
            {poll_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        if not poll_task.done():
            try:
                await dp.stop_polling()
            except Exception as exc:
                log.warning("stop_polling_failed", error=repr(exc))
                poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await poll_task
        if poll_task.done() and not poll_task.cancelled():
            poll_exc = poll_task.exception()
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        for label, coro in (
            ("pipeline_stop_failed", pipeline.stop()),
            ("bot_session_close_failed", bot.session.close()),
            ("cache_close_failed", cache.close()),
        ):
            try:
                await coro
            except Exception as exc:
                log.warning(label, error=repr(exc))
        log.info("yoink stopped", poll_error=repr(poll_exc) if poll_exc else None)
    if poll_exc is not None:
        raise poll_exc
    return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0
    except Exception:
        get_logger("yoink").exception("yoink_fatal")
        return 1


if __name__ == "__main__":
    sys.exit(main())
