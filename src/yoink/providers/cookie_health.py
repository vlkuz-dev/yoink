from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

State = Literal["OK", "EXPIRED", "UNKNOWN", "NOT_CONFIGURED"]


@dataclass(slots=True, frozen=True)
class CookiesStat:
    path: Path | None
    exists: bool
    size_bytes: int | None
    mtime: float | None


@dataclass(slots=True)
class CookieHealth:
    """In-memory health state for the single IG cookies file.

    Lifetime: one instance per process, owned by `__main__`, mutated by
    `InstagramProvider` (mark_success / mark_failure) and read by
    `/ig_status`. All mutating methods take an internal asyncio.Lock so
    concurrent worker tasks can't race on the incident flags.
    """

    _path: Path | None = None
    _last_success: float | None = None
    _last_failure: float | None = None
    _last_failure_reason: str | None = None
    _incident_open: bool = False
    _incident_notified: bool = False
    _lock: asyncio.Lock | None = field(default=None, repr=False)

    def _get_lock(self) -> asyncio.Lock:
        # Lazy-init: a running event loop may not exist at construction time
        # (Settings/CookieHealth are built before `asyncio.run`).
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def set_path(self, path: Path | None) -> None:
        self._path = path

    async def mark_success(self) -> None:
        async with self._get_lock():
            self._last_success = time.time()
            self._incident_open = False
            self._incident_notified = False

    async def mark_failure(self, reason: str) -> None:
        async with self._get_lock():
            self._last_failure = time.time()
            self._last_failure_reason = reason
            self._incident_open = True

    async def acquire_notify_slot(self) -> bool:
        """Atomically claim the right to send one DM for the current open incident.

        Returns True the first time it's called per open incident; False on
        subsequent calls until `mark_success()` clears the incident.
        """
        async with self._get_lock():
            if not self._incident_open:
                return False
            if self._incident_notified:
                return False
            self._incident_notified = True
            return True

    def stat(self) -> CookiesStat:
        if self._path is None:
            return CookiesStat(path=None, exists=False, size_bytes=None, mtime=None)
        try:
            st = self._path.stat()
        except OSError:
            return CookiesStat(path=self._path, exists=False, size_bytes=None, mtime=None)
        if not self._path.is_file():
            return CookiesStat(path=self._path, exists=False, size_bytes=None, mtime=None)
        return CookiesStat(
            path=self._path,
            exists=True,
            size_bytes=st.st_size,
            mtime=st.st_mtime,
        )

    def state(self, stat: CookiesStat | None = None) -> State:
        if self._path is None:
            return "NOT_CONFIGURED"
        if stat is None:
            stat = self.stat()
        if not stat.exists:
            return "UNKNOWN"
        if self._incident_open:
            return "EXPIRED"
        return "OK"

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def last_success(self) -> float | None:
        return self._last_success

    @property
    def last_failure(self) -> tuple[float, str] | None:
        if self._last_failure is None:
            return None
        return (self._last_failure, self._last_failure_reason or "unknown")

    @property
    def incident_open(self) -> bool:
        return self._incident_open


__all__ = ["CookieHealth", "CookiesStat", "State"]
