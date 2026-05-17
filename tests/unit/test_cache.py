from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from yoink.cache.store import CachedFile, FileIdCache, hash_url


@pytest.fixture
async def cache(tmp_path: Path) -> FileIdCache:
    store = FileIdCache(tmp_path / "yoink.sqlite")
    await store.init()
    try:
        yield store
    finally:
        await store.close()


def test_hash_url_is_stable_and_32_chars() -> None:
    h1 = hash_url("https://instagram.com/p/abc")
    h2 = hash_url("https://instagram.com/p/abc")
    h3 = hash_url("https://instagram.com/p/xyz")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 32
    assert all(c in "0123456789abcdef" for c in h1)


async def test_init_creates_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dirs" / "yoink.sqlite"
    store = FileIdCache(db_path)
    await store.init()
    try:
        assert db_path.exists()
        async with (
            aiosqlite.connect(db_path) as conn,
            conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") as cursor,
        ):
            rows = await cursor.fetchall()
        names = {row[0] for row in rows}
        assert {"cached_url", "cached_file"}.issubset(names)
    finally:
        await store.close()


async def test_init_is_idempotent(cache: FileIdCache) -> None:
    await cache.init()
    await cache.init()
    stats = await cache.stats()
    assert stats.url_count == 0


async def test_put_and_get_round_trip_preserves_order(cache: FileIdCache) -> None:
    files = [
        CachedFile(file_id="fid-0", kind="photo", mime="image/jpeg"),
        CachedFile(file_id="fid-1", kind="video", mime="video/mp4"),
        CachedFile(file_id="fid-2", kind="photo", mime=None),
    ]
    await cache.put("hash-1", "https://example.com/x", "instagram", files)
    fetched = await cache.get("hash-1")
    assert fetched is not None
    assert [f.file_id for f in fetched] == ["fid-0", "fid-1", "fid-2"]
    assert [f.kind for f in fetched] == ["photo", "video", "photo"]
    assert [f.mime for f in fetched] == ["image/jpeg", "video/mp4", None]


async def test_get_missing_returns_none(cache: FileIdCache) -> None:
    assert await cache.get("nope") is None


async def test_put_overwrites_existing(cache: FileIdCache) -> None:
    await cache.put(
        "hash-1",
        "https://example.com/old",
        "instagram",
        [CachedFile(file_id="old", kind="photo")],
    )
    await cache.put(
        "hash-1",
        "https://example.com/new",
        "instagram",
        [
            CachedFile(file_id="new-0", kind="video", mime="video/mp4"),
            CachedFile(file_id="new-1", kind="photo"),
        ],
    )
    fetched = await cache.get("hash-1")
    assert fetched is not None
    assert [f.file_id for f in fetched] == ["new-0", "new-1"]


async def test_get_updates_last_used_at(cache: FileIdCache, monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([1000, 2000])
    monkeypatch.setattr("yoink.cache.store.time.time", lambda: next(times))
    await cache.put(
        "hash-1",
        "https://example.com/x",
        "instagram",
        [CachedFile(file_id="fid", kind="photo")],
    )
    await cache.get("hash-1")
    conn = cache._require_conn()
    async with conn.execute(
        "SELECT created_at, last_used_at FROM cached_url WHERE url_hash = ?",
        ("hash-1",),
    ) as cursor:
        row = await cursor.fetchone()
    assert row == (1000, 2000)


async def test_flush_clears_everything(cache: FileIdCache) -> None:
    await cache.put(
        "h1",
        "https://example.com/1",
        "instagram",
        [CachedFile(file_id="a", kind="photo")],
    )
    await cache.put(
        "h2",
        "https://example.com/2",
        "instagram",
        [CachedFile(file_id="b", kind="photo")],
    )
    cleared = await cache.flush()
    assert cleared == 2
    stats = await cache.stats()
    assert stats.url_count == 0
    assert stats.file_count == 0
    assert await cache.get("h1") is None


async def test_stats_returns_counts(cache: FileIdCache) -> None:
    await cache.put(
        "h1",
        "https://example.com/1",
        "instagram",
        [
            CachedFile(file_id="a", kind="photo"),
            CachedFile(file_id="b", kind="video"),
        ],
    )
    await cache.put(
        "h2",
        "https://example.com/2",
        "instagram",
        [CachedFile(file_id="c", kind="photo")],
    )
    stats = await cache.stats()
    assert stats.url_count == 2
    assert stats.file_count == 3


async def test_concurrent_puts_do_not_corrupt(cache: FileIdCache) -> None:
    async def put_one(idx: int) -> None:
        await cache.put(
            f"hash-{idx}",
            f"https://example.com/{idx}",
            "instagram",
            [
                CachedFile(file_id=f"fid-{idx}-0", kind="photo"),
                CachedFile(file_id=f"fid-{idx}-1", kind="video"),
            ],
        )

    await asyncio.gather(*(put_one(i) for i in range(20)))
    stats = await cache.stats()
    assert stats.url_count == 20
    assert stats.file_count == 40
    for i in range(20):
        fetched = await cache.get(f"hash-{i}")
        assert fetched is not None
        assert [f.file_id for f in fetched] == [f"fid-{i}-0", f"fid-{i}-1"]


async def test_require_conn_raises_before_init(tmp_path: Path) -> None:
    store = FileIdCache(tmp_path / "yoink.sqlite")
    with pytest.raises(RuntimeError):
        await store.get("h")


async def test_put_with_empty_files_keeps_url_row(cache: FileIdCache) -> None:
    await cache.put("hash-empty", "https://example.com/none", "instagram", [])
    fetched = await cache.get("hash-empty")
    assert fetched == []
    stats = await cache.stats()
    assert stats.url_count == 1
    assert stats.file_count == 0
