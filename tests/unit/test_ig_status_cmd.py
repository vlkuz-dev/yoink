from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from yoink.admin.commands import cmd_ig_status
from yoink.config import Settings
from yoink.providers.cookie_health import CookieHealth

_ADMIN_UID = 42
_NON_ADMIN_UID = 7


def _settings() -> Settings:
    return Settings(
        bot_token="x" * 8,
        admin_ids=frozenset({_ADMIN_UID}),
        cache_db=Path("/tmp/yoink-test-cache.sqlite"),
        workdir=Path("/tmp/yoink-test-workdir"),
    )


def _msg(user_id: int) -> Any:
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=1),
        reply=AsyncMock(return_value=None),
    )


@pytest.mark.asyncio
async def test_ig_status_renders_not_configured() -> None:
    msg = _msg(_ADMIN_UID)
    await cmd_ig_status(msg, _settings(), CookieHealth())
    text = msg.reply.await_args.args[0]
    assert "state: NOT_CONFIGURED" in text
    assert "(not configured)" in text


@pytest.mark.asyncio
async def test_ig_status_renders_unknown_when_path_missing(tmp_path: Path) -> None:
    h = CookieHealth()
    h.set_path(tmp_path / "missing.txt")
    msg = _msg(_ADMIN_UID)
    await cmd_ig_status(msg, _settings(), h)
    text = msg.reply.await_args.args[0]
    assert "state: UNKNOWN" in text
    assert "exists: no" in text


@pytest.mark.asyncio
async def test_ig_status_renders_ok(tmp_path: Path) -> None:
    cookies = tmp_path / "c.txt"
    cookies.write_bytes(b"hello")
    h = CookieHealth()
    h.set_path(cookies)
    await h.mark_success()

    msg = _msg(_ADMIN_UID)
    await cmd_ig_status(msg, _settings(), h)
    text = msg.reply.await_args.args[0]
    assert "state: OK" in text
    assert "exists: yes" in text
    assert "size: 5 bytes" in text
    assert "last_success:" in text
    assert "ago)" in text
    assert "last_failure: (none)" in text


@pytest.mark.asyncio
async def test_ig_status_renders_expired(tmp_path: Path) -> None:
    cookies = tmp_path / "c.txt"
    cookies.write_bytes(b"x")
    h = CookieHealth()
    h.set_path(cookies)
    await h.mark_failure("login_required")

    msg = _msg(_ADMIN_UID)
    await cmd_ig_status(msg, _settings(), h)
    text = msg.reply.await_args.args[0]
    assert "state: EXPIRED" in text
    assert "login_required" in text


@pytest.mark.asyncio
async def test_ig_status_non_admin_silent(tmp_path: Path) -> None:
    h = CookieHealth()
    h.set_path(tmp_path / "anything.txt")
    msg = _msg(_NON_ADMIN_UID)
    await cmd_ig_status(msg, _settings(), h)
    msg.reply.assert_not_awaited()
