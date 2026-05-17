from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot
    from structlog.stdlib import BoundLogger

    from yoink.providers.cookie_health import CookieHealth


@dataclass(slots=True)
class AdminNotifier:
    """Sends one-shot incident DMs to configured admins.

    Stateless w.r.t. dedupe — dedupe lives on CookieHealth.acquire_notify_slot().
    On any per-admin send failure (chat not found, blocked bot, etc.) we log
    and continue: a partial fan-out is still useful.
    """

    bot: Bot
    admin_ids: frozenset[int]
    log: BoundLogger

    async def notify_cookies_expired(
        self,
        health: CookieHealth,
        *,
        reason: str,
    ) -> None:
        if not await health.acquire_notify_slot():
            return
        text = (
            f"yoink: Instagram cookies expired ({reason}).\n"
            "Run /ig_status for details. Re-export cookies and overwrite the "
            "configured file to recover."
        )
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except Exception:
                self.log.exception("notifier_send_failed", admin_id=admin_id)


__all__ = ["AdminNotifier"]
