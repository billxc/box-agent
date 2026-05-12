"""Standalone Telegram push notifier.

Subscribes to the EventBus and POSTs matching events directly to the
Telegram Bot API (no aiogram dependency, no coupling to chat-bot tokens).

Filtering: by level (default error/notify) and optional category prefixes.
Per design decision (yait #90 / Q4): no rate-limiting — every matching event
yields one Telegram message. The user explicitly opted out of throttling.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import aiohttp

from .bus import EventBus
from .models import Event

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"


def _format_message(event: Event) -> str:
    parts = [
        f"[{event.level.upper()}] {event.category}",
        event.message,
    ]
    if event.bot:
        parts.append(f"bot: {event.bot}")
    if event.origin_machine:
        parts.append(f"@{event.origin_machine}")
    return "\n".join(parts)


def _matches_category(category: str, prefixes: Iterable[str]) -> bool:
    prefixes = list(prefixes)
    if not prefixes:
        return True
    for prefix in prefixes:
        if category == prefix or category.startswith(prefix + "."):
            return True
    return False


class TelegramNotifier:
    def __init__(
        self,
        *,
        token: str,
        chat_id: str,
        levels: Iterable[str],
        categories: Iterable[str] = (),
        loop: asyncio.AbstractEventLoop | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._token = token
        self._chat_id = str(chat_id)
        self._levels = {str(level).lower() for level in levels}
        self._categories = list(categories)
        self._loop = loop
        self._session = session
        self._owns_session = session is None

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def attach(self, bus: EventBus) -> None:
        if not self.enabled:
            return
        bus.subscribe(self._on_event)

    def detach(self, bus: EventBus) -> None:
        bus.unsubscribe(self._on_event)

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    # ---------- internal ----------

    def _on_event(self, event: Event) -> None:
        if event.level.lower() not in self._levels:
            return
        if not _matches_category(event.category, self._categories):
            return
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.debug("notifier: no running loop, dropping event")
                return
        loop.create_task(self._deliver(event))

    async def _deliver(self, event: Event) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        url = f"{API_BASE}/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": _format_message(event),
            "disable_web_page_preview": True,
        }
        try:
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status >= 400:
                    body = await response.text()
                    logger.warning(
                        "telegram notify failed: status=%s body=%s",
                        response.status, body[:200],
                    )
        except Exception as exc:
            logger.warning("telegram notify exception: %r", exc)
