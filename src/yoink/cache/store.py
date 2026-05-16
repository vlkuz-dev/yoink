from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from yoink.core.models import MediaKind

if TYPE_CHECKING:
    from collections.abc import Iterable

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_HASH_LEN = 32


def hash_url(normalized_url: str) -> str:
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    return digest[:_HASH_LEN]


@dataclass(slots=True, kw_only=True)
class CachedFile:
    file_id: str
    kind: MediaKind
    mime: str | None = None


@dataclass(slots=True, kw_only=True)
class CacheStats:
    url_count: int
    file_count: int


class FileIdCache:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def init(self) -> None:
        if self._conn is not None:
            return
        path = Path(self._db_path)
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._db_path)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        schema = _SCHEMA_PATH.read_text(encoding="utf-8")
        await conn.executescript(schema)
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    async def get(self, url_hash: str) -> list[CachedFile] | None:
        conn = self._require_conn()
        async with conn.execute(
            "SELECT 1 FROM cached_url WHERE url_hash = ?",
            (url_hash,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        async with conn.execute(
            "SELECT file_id, kind, mime FROM cached_file WHERE url_hash = ? ORDER BY position ASC",
            (url_hash,),
        ) as cursor:
            file_rows = await cursor.fetchall()
        async with self._write_lock:
            await conn.execute(
                "UPDATE cached_url SET last_used_at = ? WHERE url_hash = ?",
                (int(time.time()), url_hash),
            )
            await conn.commit()
        return [
            CachedFile(file_id=row[0], kind=row[1], mime=row[2])
            for row in file_rows
        ]

    async def put(
        self,
        url_hash: str,
        source_url: str,
        provider: str,
        files: Iterable[CachedFile],
    ) -> None:
        conn = self._require_conn()
        now = int(time.time())
        items = list(files)
        async with self._write_lock:
            await conn.execute(
                """
                INSERT INTO cached_url (url_hash, source_url, provider, created_at, last_used_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(url_hash) DO UPDATE SET
                    source_url = excluded.source_url,
                    provider = excluded.provider,
                    last_used_at = excluded.last_used_at
                """,
                (url_hash, source_url, provider, now, now),
            )
            await conn.execute("DELETE FROM cached_file WHERE url_hash = ?", (url_hash,))
            if items:
                await conn.executemany(
                    "INSERT INTO cached_file (url_hash, position, file_id, kind, mime) VALUES (?, ?, ?, ?, ?)",
                    [
                        (url_hash, position, item.file_id, item.kind, item.mime)
                        for position, item in enumerate(items)
                    ],
                )
            await conn.commit()

    async def flush(self) -> int:
        conn = self._require_conn()
        async with self._write_lock:
            async with conn.execute("SELECT COUNT(*) FROM cached_url") as cursor:
                row = await cursor.fetchone()
            count = int(row[0]) if row is not None else 0
            await conn.execute("DELETE FROM cached_file")
            await conn.execute("DELETE FROM cached_url")
            await conn.commit()
        return count

    async def stats(self) -> CacheStats:
        conn = self._require_conn()
        async with conn.execute("SELECT COUNT(*) FROM cached_url") as cursor:
            url_row = await cursor.fetchone()
        async with conn.execute("SELECT COUNT(*) FROM cached_file") as cursor:
            file_row = await cursor.fetchone()
        return CacheStats(
            url_count=int(url_row[0]) if url_row is not None else 0,
            file_count=int(file_row[0]) if file_row is not None else 0,
        )

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("FileIdCache is not initialized; call init() first")
        return self._conn
