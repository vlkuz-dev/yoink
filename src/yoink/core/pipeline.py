from __future__ import annotations

import asyncio
import contextlib
import secrets
import shutil
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import urlsplit

from aiogram.exceptions import TelegramBadRequest

from yoink.cache.store import hash_url
from yoink.core.errors import MediaTooLarge, ProviderError, ProviderTransientError
from yoink.core.models import Job
from yoink.downloader.safety import host_in_allowlist
from yoink.extractor.urls import extract_urls
from yoink.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from aiogram.types import Message

    from yoink.cache.store import FileIdCache
    from yoink.core.rate_limiter import TokenBucketLimiter
    from yoink.core.registry import ProviderRegistry
    from yoink.uploader.telegram import TelegramUploader

    SleepFn = Callable[[float], Awaitable[None]]


T = TypeVar("T")

_log = get_logger(__name__)


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base: float = 1.0,
    factor: float = 4.0,
    retry_on: tuple[type[BaseException], ...] = (ProviderTransientError,),
    sleep: SleepFn | None = None,
) -> T:
    """Retry an async callable with exponential backoff.

    Sleeps `base * factor**i` seconds after attempt i fails. Only exceptions
    in `retry_on` trigger retry; everything else re-raises immediately.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    sleep_fn: SleepFn = sleep if sleep is not None else asyncio.sleep
    last_exc: BaseException | None = None
    for i in range(attempts):
        try:
            return await fn()
        except BaseException as exc:
            if not isinstance(exc, retry_on):
                raise
            last_exc = exc
            if i == attempts - 1:
                break
            await sleep_fn(base * (factor**i))
    assert last_exc is not None
    raise last_exc


class Pipeline:
    """Wires registry + cache + rate_limiter + uploader + queue + workers.

    `submit(message)` is called from the aiogram handler. Cache hits are
    served inline (no queue); misses enqueue a Job. N worker tasks consume
    jobs, fetch via provider, upload, cache, and clean up the workdir.
    """

    def __init__(
        self,
        *,
        registry: ProviderRegistry,
        cache: FileIdCache,
        rate_limiter: TokenBucketLimiter,
        uploader: TelegramUploader,
        workdir_root: Path,
        workers: int = 4,
        queue_maxsize: int = 64,
        retry_attempts: int = 3,
        retry_base_s: float = 1.0,
        retry_factor: float = 4.0,
        retry_sleep: SleepFn | None = None,
        allowlist: frozenset[str] | None = None,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be >= 1")
        self._registry = registry
        self._cache = cache
        self._rate_limiter = rate_limiter
        self._uploader = uploader
        self._workdir_root = workdir_root
        self._workers = workers
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=queue_maxsize)
        self._tasks: list[asyncio.Task[None]] = []
        self._retry_attempts = retry_attempts
        self._retry_base_s = retry_base_s
        self._retry_factor = retry_factor
        self._retry_sleep = retry_sleep
        self._allowlist = allowlist

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def worker_count(self) -> int:
        return len(self._tasks)

    async def submit(self, message: Message) -> None:
        chat = message.chat
        if chat is None:
            return
        chat_id = chat.id
        user = getattr(message, "from_user", None)
        user_id = user.id if user is not None else 0
        reply_to = getattr(message, "message_id", None)

        urls = extract_urls(message)
        for url in urls:
            if self._allowlist is not None and not self._url_in_allowlist(url):
                _log.info("allowlist_blocked", url=url, chat_id=chat_id)
                continue
            provider = self._registry.find(url)
            if provider is None:
                continue
            if not self._rate_limiter.try_acquire(chat_id):
                _log.info("rate_limited", chat_id=chat_id, url=url)
                continue
            url_hash = hash_url(url)
            cached = await self._cache.get(url_hash)
            if cached:
                try:
                    await self._uploader.send_cached(
                        chat_id=chat_id,
                        reply_to=reply_to,
                        files=cached,
                    )
                except (TelegramBadRequest, MediaTooLarge):
                    _log.exception("cache_resend_failed", url=url, chat_id=chat_id)
                continue
            job = Job(
                chat_id=chat_id,
                reply_to_message_id=reply_to,
                url=url,
                user_id=user_id,
                correlation_id=secrets.token_hex(8),
            )
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull:
                _log.warning("queue_full_drop", url=url, chat_id=chat_id)

    async def _worker(self, worker_id: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._process(job)
            except (ProviderError, MediaTooLarge, TelegramBadRequest):
                _log.exception(
                    "job_failed",
                    url=job.url,
                    chat_id=job.chat_id,
                    correlation_id=job.correlation_id,
                    worker=worker_id,
                )
            except Exception:
                _log.exception(
                    "job_unexpected_error",
                    url=job.url,
                    chat_id=job.chat_id,
                    correlation_id=job.correlation_id,
                    worker=worker_id,
                )
            finally:
                self._queue.task_done()

    async def _process(self, job: Job) -> None:
        provider = self._registry.find(job.url)
        if provider is None:
            _log.warning(
                "provider_unavailable",
                url=job.url,
                correlation_id=job.correlation_id,
            )
            return
        workdir = self._workdir_root / job.correlation_id
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            package = await retry_async(
                lambda: provider.fetch(job.url, workdir),
                attempts=self._retry_attempts,
                base=self._retry_base_s,
                factor=self._retry_factor,
                sleep=self._retry_sleep,
            )
            files = await self._uploader.send(
                chat_id=job.chat_id,
                reply_to=job.reply_to_message_id,
                package=package,
            )
            await self._cache.put(
                hash_url(job.url),
                job.url,
                package.provider,
                files,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def start(self) -> None:
        if self._tasks:
            return
        self._workdir_root.mkdir(parents=True, exist_ok=True)
        for i in range(self._workers):
            self._tasks.append(
                asyncio.create_task(self._worker(i), name=f"yoink-worker-{i}"),
            )

    async def stop(self) -> None:
        tasks = self._tasks
        self._tasks = []
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def join(self) -> None:
        """Wait until all enqueued jobs are processed (test helper)."""
        await self._queue.join()

    def _url_in_allowlist(self, url: str) -> bool:
        if self._allowlist is None:
            return True
        host = urlsplit(url).hostname
        if not host:
            return False
        return host_in_allowlist(host, self._allowlist)
