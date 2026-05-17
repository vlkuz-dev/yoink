from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from yoink.providers.cookie_health import CookieHealth


def test_state_not_configured_when_no_path() -> None:
    h = CookieHealth()
    assert h.state() == "NOT_CONFIGURED"


def test_state_unknown_when_path_missing(tmp_path: Path) -> None:
    h = CookieHealth()
    h.set_path(tmp_path / "nope.txt")
    assert h.state() == "UNKNOWN"


def test_state_ok_when_file_exists_and_no_failure(tmp_path: Path) -> None:
    cookies = tmp_path / "c.txt"
    cookies.write_bytes(b"x")
    h = CookieHealth()
    h.set_path(cookies)
    assert h.state() == "OK"


async def test_state_expired_after_failure(tmp_path: Path) -> None:
    cookies = tmp_path / "c.txt"
    cookies.write_bytes(b"x")
    h = CookieHealth()
    h.set_path(cookies)
    await h.mark_failure("login_required")
    assert h.state() == "EXPIRED"
    lf = h.last_failure
    assert lf is not None
    assert lf[1] == "login_required"


async def test_success_resets_incident(tmp_path: Path) -> None:
    cookies = tmp_path / "c.txt"
    cookies.write_bytes(b"x")
    h = CookieHealth()
    h.set_path(cookies)
    await h.mark_failure("login_required")
    assert h.incident_open
    await h.mark_success()
    assert not h.incident_open
    assert h.state() == "OK"


async def test_acquire_notify_slot_only_once_per_incident() -> None:
    h = CookieHealth()
    h.set_path(Path("/tmp/dummy"))
    # No incident open yet -> no slot.
    assert await h.acquire_notify_slot() is False
    await h.mark_failure("login_required")
    assert await h.acquire_notify_slot() is True
    assert await h.acquire_notify_slot() is False
    # New cycle: success then failure -> slot available again.
    await h.mark_success()
    await h.mark_failure("checkpoint_required")
    assert await h.acquire_notify_slot() is True


def test_stat_missing_file_returns_not_exists(tmp_path: Path) -> None:
    h = CookieHealth()
    h.set_path(tmp_path / "missing.txt")
    s = h.stat()
    assert s.exists is False
    assert s.size_bytes is None
    assert s.mtime is None


def test_stat_existing_file_returns_size_and_mtime(tmp_path: Path) -> None:
    cookies = tmp_path / "c.txt"
    cookies.write_bytes(b"hello")
    h = CookieHealth()
    h.set_path(cookies)
    s = h.stat()
    assert s.exists is True
    assert s.size_bytes == 5
    assert s.mtime is not None


def test_stat_no_path_returns_not_configured() -> None:
    s = CookieHealth().stat()
    assert s.path is None
    assert s.exists is False


async def test_concurrent_mark_failure_does_not_double_open() -> None:
    h = CookieHealth()
    h.set_path(Path("/tmp/dummy"))
    await asyncio.gather(
        *(h.mark_failure("login_required") for _ in range(20)),
    )
    # Many failures, but only one notify slot should be claimable.
    first = await h.acquire_notify_slot()
    second = await h.acquire_notify_slot()
    assert first is True
    assert second is False


@pytest.mark.asyncio
async def test_directory_at_path_is_not_a_file(tmp_path: Path) -> None:
    """If the configured path is a directory (operator error), stat() reports
    exists=False so state() is UNKNOWN, not OK."""
    h = CookieHealth()
    h.set_path(tmp_path)
    s = h.stat()
    assert s.exists is False
    assert h.state() == "UNKNOWN"
