from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from yoink.cache.store import CachedFile, FileIdCache, hash_url
from yoink.core.errors import ProviderError, ProviderTransientError
from yoink.core.models import MediaItem, MediaPackage
from yoink.core.pipeline import Pipeline, retry_async
from yoink.core.rate_limiter import TokenBucketLimiter
from yoink.core.registry import ProviderRegistry

if TYPE_CHECKING:
    from collections.abc import Iterable


class FakeProvider:
    name: str = "instagram"
    domains: frozenset[str] = frozenset({"instagram.com", "www.instagram.com"})

    def __init__(
        self,
        *,
        media_root: Path,
        fail_urls: Iterable[str] = (),
        transient_urls: Iterable[str] = (),
    ) -> None:
        self.fetch_calls: list[str] = []
        self._media_root = media_root
        self._fail = set(fail_urls)
        self._transient = dict.fromkeys(transient_urls, 0)

    def can_handle(self, url: str) -> bool:
        return "instagram.com" in url

    async def fetch(self, url: str, workdir: Path) -> MediaPackage:
        self.fetch_calls.append(url)
        if url in self._fail:
            raise ProviderError("permanent failure", url=url)
        if url in self._transient and self._transient[url] == 0:
            self._transient[url] += 1
            raise ProviderTransientError("transient", url=url)
        path = workdir / "media.jpg"
        path.write_bytes(b"\xff\xd8\xff\xd9")
        return MediaPackage(
            source_url=url,
            provider=self.name,
            items=[MediaItem(path=path, kind="photo", mime="image/jpeg")],
        )


@dataclass
class FakeUploader:
    sends: list[tuple[int, int | None, MediaPackage]] = field(default_factory=list)
    cached_sends: list[tuple[int, int | None, list[CachedFile]]] = field(default_factory=list)
    next_file_id: int = 0

    async def send(
        self,
        *,
        chat_id: int,
        reply_to: int | None,
        package: MediaPackage,
    ) -> list[CachedFile]:
        self.sends.append((chat_id, reply_to, package))
        out: list[CachedFile] = []
        for item in package.items:
            self.next_file_id += 1
            out.append(
                CachedFile(
                    file_id=f"FID-{self.next_file_id}",
                    kind=item.kind,
                    mime=item.mime,
                ),
            )
        return out

    async def send_cached(
        self,
        *,
        chat_id: int,
        reply_to: int | None,
        files: list[CachedFile],
    ) -> list[CachedFile]:
        self.cached_sends.append((chat_id, reply_to, list(files)))
        return list(files)


def _make_message(url: str, *, chat_id: int = 1001, user_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        text=url,
        caption=None,
        entities=None,
        caption_entities=None,
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id),
        message_id=42,
    )


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _build_pipeline(
    tmp_path: Path,
    *,
    provider: FakeProvider,
    uploader: FakeUploader,
    rate_per_min: int = 600,
    burst: int = 600,
    workers: int = 2,
    retry_attempts: int = 3,
) -> tuple[Pipeline, FileIdCache, ProviderRegistry]:
    cache = FileIdCache(tmp_path / "cache.sqlite")
    await cache.init()
    registry = ProviderRegistry()
    registry.register(provider)
    limiter = TokenBucketLimiter(rate_per_min, burst=burst)

    async def fast_sleep(_s: float) -> None:
        return None

    pipeline = Pipeline(
        registry=registry,
        cache=cache,
        rate_limiter=limiter,
        uploader=uploader,  # type: ignore[arg-type]
        workdir_root=tmp_path / "work",
        workers=workers,
        retry_attempts=retry_attempts,
        retry_sleep=fast_sleep,
    )
    await pipeline.start()
    return pipeline, cache, registry


@pytest.mark.asyncio
async def test_retry_async_success_first_try() -> None:
    calls = 0
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    async def fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    result = await retry_async(fn, attempts=3, base=1.0, factor=4.0, sleep=sleep)
    assert result == "ok"
    assert calls == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_retry_async_succeeds_after_transient_failures() -> None:
    calls = 0
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    async def fn() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ProviderTransientError("again")
        return "ok"

    result = await retry_async(fn, attempts=3, base=1.0, factor=4.0, sleep=sleep)
    assert result == "ok"
    assert calls == 3
    assert sleeps == [1.0, 4.0]


@pytest.mark.asyncio
async def test_retry_async_exhausts_then_reraises() -> None:
    calls = 0

    async def sleep(_s: float) -> None:
        return None

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise ProviderTransientError("nope")

    with pytest.raises(ProviderTransientError):
        await retry_async(fn, attempts=3, base=1.0, factor=4.0, sleep=sleep)
    assert calls == 3


@pytest.mark.asyncio
async def test_retry_async_permanent_not_retried() -> None:
    calls = 0

    async def sleep(_s: float) -> None:
        return None

    async def fn() -> None:
        nonlocal calls
        calls += 1
        raise ProviderError("no")

    with pytest.raises(ProviderError):
        await retry_async(fn, attempts=3, sleep=sleep)
    assert calls == 1


@pytest.mark.asyncio
async def test_pipeline_caches_on_second_submit(tmp_path: Path) -> None:
    provider = FakeProvider(media_root=tmp_path)
    uploader = FakeUploader()
    pipeline, cache, _ = await _build_pipeline(
        tmp_path, provider=provider, uploader=uploader,
    )
    try:
        url = "https://www.instagram.com/p/abc123/"
        await pipeline.submit(_make_message(url))
        await pipeline.join()
        assert len(provider.fetch_calls) == 1
        assert len(uploader.sends) == 1
        assert uploader.cached_sends == []
        # cache populated
        stored = await cache.get(hash_url(url))
        assert stored is not None and len(stored) == 1

        await pipeline.submit(_make_message(url))
        await pipeline.join()

        assert len(provider.fetch_calls) == 1  # no extra fetch
        assert len(uploader.sends) == 1
        assert len(uploader.cached_sends) == 1
        assert uploader.cached_sends[0][2][0].file_id == stored[0].file_id
    finally:
        await pipeline.stop()
        await cache.close()


@pytest.mark.asyncio
async def test_pipeline_silently_skips_unknown_url(tmp_path: Path) -> None:
    provider = FakeProvider(media_root=tmp_path)
    uploader = FakeUploader()
    pipeline, cache, _ = await _build_pipeline(
        tmp_path, provider=provider, uploader=uploader,
    )
    try:
        await pipeline.submit(_make_message("https://example.com/nothing"))
        await pipeline.join()
        assert provider.fetch_calls == []
        assert uploader.sends == []
        assert uploader.cached_sends == []
    finally:
        await pipeline.stop()
        await cache.close()


@pytest.mark.asyncio
async def test_pipeline_worker_survives_provider_error(tmp_path: Path) -> None:
    bad_url = "https://www.instagram.com/p/bad/"
    good_url = "https://www.instagram.com/p/good/"
    provider = FakeProvider(media_root=tmp_path, fail_urls=[bad_url])
    uploader = FakeUploader()
    pipeline, cache, _ = await _build_pipeline(
        tmp_path,
        provider=provider,
        uploader=uploader,
        workers=1,
    )
    try:
        await pipeline.submit(_make_message(bad_url))
        await pipeline.submit(_make_message(good_url))
        await pipeline.join()

        assert bad_url in provider.fetch_calls
        assert good_url in provider.fetch_calls
        assert len(uploader.sends) == 1
        assert uploader.sends[0][2].source_url == good_url
    finally:
        await pipeline.stop()
        await cache.close()


@pytest.mark.asyncio
async def test_pipeline_retries_transient_then_succeeds(tmp_path: Path) -> None:
    url = "https://www.instagram.com/p/trans/"
    provider = FakeProvider(media_root=tmp_path, transient_urls=[url])
    uploader = FakeUploader()
    pipeline, cache, _ = await _build_pipeline(
        tmp_path,
        provider=provider,
        uploader=uploader,
        workers=1,
        retry_attempts=3,
    )
    try:
        await pipeline.submit(_make_message(url))
        await pipeline.join()
        assert provider.fetch_calls == [url, url]
        assert len(uploader.sends) == 1
    finally:
        await pipeline.stop()
        await cache.close()


@pytest.mark.asyncio
async def test_pipeline_workdir_cleaned_after_job(tmp_path: Path) -> None:
    provider = FakeProvider(media_root=tmp_path)
    uploader = FakeUploader()
    pipeline, cache, _ = await _build_pipeline(
        tmp_path, provider=provider, uploader=uploader, workers=1,
    )
    try:
        await pipeline.submit(_make_message("https://www.instagram.com/p/clean/"))
        await pipeline.join()
        work_root = tmp_path / "work"
        # root persists; per-job subdirs are removed
        if work_root.exists():
            subdirs = [p for p in work_root.iterdir() if p.is_dir()]
            assert subdirs == []
    finally:
        await pipeline.stop()
        await cache.close()


@pytest.mark.asyncio
async def test_pipeline_sweeps_orphan_workdirs_on_start(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    work_root.mkdir()
    orphan_a = work_root / "abc123"
    orphan_a.mkdir()
    (orphan_a / "stale.bin").write_bytes(b"x")
    orphan_b = work_root / "def456"
    orphan_b.mkdir()
    heartbeat = work_root / ".heartbeat"
    heartbeat.write_text("")

    provider = FakeProvider(media_root=tmp_path)
    uploader = FakeUploader()
    cache = FileIdCache(tmp_path / "cache.sqlite")
    await cache.init()
    registry = ProviderRegistry()
    registry.register(provider)
    limiter = TokenBucketLimiter(rate_per_min=600, burst=600)
    pipeline = Pipeline(
        registry=registry,
        cache=cache,
        rate_limiter=limiter,
        uploader=uploader,  # type: ignore[arg-type]
        workdir_root=work_root,
        workers=1,
    )
    await pipeline.start()
    try:
        assert not orphan_a.exists()
        assert not orphan_b.exists()
        assert heartbeat.exists()  # preserved
    finally:
        await pipeline.stop()
        await cache.close()


@pytest.mark.asyncio
async def test_pipeline_writes_heartbeat(tmp_path: Path) -> None:
    work_root = tmp_path / "work"
    provider = FakeProvider(media_root=tmp_path)
    uploader = FakeUploader()
    cache = FileIdCache(tmp_path / "cache.sqlite")
    await cache.init()
    registry = ProviderRegistry()
    registry.register(provider)
    limiter = TokenBucketLimiter(rate_per_min=600, burst=600)
    pipeline = Pipeline(
        registry=registry,
        cache=cache,
        rate_limiter=limiter,
        uploader=uploader,  # type: ignore[arg-type]
        workdir_root=work_root,
        workers=1,
        heartbeat_interval_s=0.01,
    )
    await pipeline.start()
    try:
        heartbeat = work_root / ".heartbeat"
        for _ in range(50):
            if heartbeat.exists():
                break
            await asyncio.sleep(0.01)
        assert heartbeat.exists()
    finally:
        await pipeline.stop()
        await cache.close()


@pytest.mark.asyncio
async def test_pipeline_rejects_unsafe_url(tmp_path: Path) -> None:
    provider = FakeProvider(media_root=tmp_path)
    uploader = FakeUploader()
    pipeline, cache, _ = await _build_pipeline(
        tmp_path, provider=provider, uploader=uploader, workers=1,
    )
    try:
        # literal private-range IP — must be rejected before provider lookup
        await pipeline.submit(_make_message("http://127.0.0.1/p/abc"))
        # userinfo in URL — rejected
        await pipeline.submit(_make_message("https://user:pwd@www.instagram.com/p/abc"))
        # non-standard port — rejected
        await pipeline.submit(_make_message("https://www.instagram.com:8443/p/abc"))
        await pipeline.join()
        assert provider.fetch_calls == []
        assert uploader.sends == []
    finally:
        await pipeline.stop()
        await cache.close()


@pytest.mark.asyncio
async def test_pipeline_rate_limit_drops_url(tmp_path: Path) -> None:
    provider = FakeProvider(media_root=tmp_path)
    uploader = FakeUploader()
    cache = FileIdCache(tmp_path / "cache.sqlite")
    await cache.init()
    registry = ProviderRegistry()
    registry.register(provider)
    limiter = TokenBucketLimiter(rate_per_min=60, burst=1)

    async def fast_sleep(_s: float) -> None:
        return None

    pipeline = Pipeline(
        registry=registry,
        cache=cache,
        rate_limiter=limiter,
        uploader=uploader,  # type: ignore[arg-type]
        workdir_root=tmp_path / "work",
        workers=1,
        retry_sleep=fast_sleep,
    )
    await pipeline.start()
    try:
        u1 = "https://www.instagram.com/p/one/"
        u2 = "https://www.instagram.com/p/two/"
        await pipeline.submit(_make_message(u1))
        await pipeline.submit(_make_message(u2))  # dropped: bucket empty
        await pipeline.join()
        assert provider.fetch_calls == [u1]
    finally:
        await pipeline.stop()
        await cache.close()
