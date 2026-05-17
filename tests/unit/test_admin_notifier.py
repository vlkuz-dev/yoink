from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from yoink.admin.notifier import AdminNotifier
from yoink.providers.cookie_health import CookieHealth


def _make_notifier(admin_ids: frozenset[int]) -> tuple[AdminNotifier, Any]:
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=None)
    log = MagicMock()
    notifier = AdminNotifier(bot=bot, admin_ids=admin_ids, log=log)
    return notifier, bot


@pytest.mark.asyncio
async def test_notify_sends_dm_to_each_admin() -> None:
    notifier, bot = _make_notifier(frozenset({1, 2, 3}))
    health = CookieHealth()
    health.set_path(Path("/tmp/dummy"))
    await health.mark_failure("login_required")

    await notifier.notify_cookies_expired(health, reason="login_required")

    sent_to = sorted(call.args[0] for call in bot.send_message.await_args_list)
    assert sent_to == [1, 2, 3]


@pytest.mark.asyncio
async def test_notify_dedupes_on_open_incident() -> None:
    notifier, bot = _make_notifier(frozenset({1}))
    health = CookieHealth()
    health.set_path(Path("/tmp/dummy"))
    await health.mark_failure("login_required")

    await notifier.notify_cookies_expired(health, reason="login_required")
    await notifier.notify_cookies_expired(health, reason="login_required")

    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_notify_renotifies_after_success_cycle() -> None:
    notifier, bot = _make_notifier(frozenset({1}))
    health = CookieHealth()
    health.set_path(Path("/tmp/dummy"))

    await health.mark_failure("login_required")
    await notifier.notify_cookies_expired(health, reason="login_required")
    await health.mark_success()
    await health.mark_failure("checkpoint_required")
    await notifier.notify_cookies_expired(health, reason="checkpoint_required")

    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_notify_per_admin_failure_continues() -> None:
    notifier, bot = _make_notifier(frozenset({1, 2, 3}))
    health = CookieHealth()
    health.set_path(Path("/tmp/dummy"))
    await health.mark_failure("login_required")

    async def flaky(chat_id: int, _text: str) -> None:
        if chat_id == 2:
            raise RuntimeError("blocked")

    bot.send_message = AsyncMock(side_effect=flaky)

    await notifier.notify_cookies_expired(health, reason="login_required")

    sent_to = sorted(call.args[0] for call in bot.send_message.await_args_list)
    assert sent_to == [1, 2, 3]


@pytest.mark.asyncio
async def test_notify_no_admins_noop() -> None:
    notifier, bot = _make_notifier(frozenset())
    health = CookieHealth()
    health.set_path(Path("/tmp/dummy"))
    await health.mark_failure("login_required")

    await notifier.notify_cookies_expired(health, reason="login_required")

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_does_nothing_when_no_incident_open() -> None:
    """acquire_notify_slot returns False when incident_open is False — no DMs."""
    notifier, bot = _make_notifier(frozenset({1}))
    health = CookieHealth()
    # No mark_failure call — incident is not open.
    await notifier.notify_cookies_expired(health, reason="login_required")
    bot.send_message.assert_not_awaited()
