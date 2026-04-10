"""Watchdog — monitors backend process liveness and triggers restart."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

RESTART_DELAY = 5.0  # seconds before restart to avoid rapid loops


@dataclass
class Watchdog:
    cli_process: object
    channel: object
    chat_id: str
    bot_name: str
    on_restart: Callable[[], Awaitable[None]]
    check_interval: float = 30.0
    restart_delay: float = RESTART_DELAY

    async def run_once(self) -> None:
        """Single watchdog check cycle."""
        state = getattr(self.cli_process, "state", "unknown")
        if state == "dead":
            logger.warning(
                "Bot '%s' backend is dead, restarting...",
                self.bot_name,
            )
            try:
                await self.channel.send_text(
                    self.chat_id,
                    f"Bot '{self.bot_name}' process died. "
                    f"Restarting in {self.restart_delay:.0f}s...",
                )
            except Exception as e:
                logger.error("Failed to notify about restart: %s", e)

            await asyncio.sleep(self.restart_delay)
            await self.on_restart()
            logger.info("Bot '%s' restarted", self.bot_name)

    async def run_forever(self) -> None:
        """Run watchdog check loop."""
        while True:
            try:
                await self.run_once()
            except Exception as e:
                logger.error("Watchdog error: %s", e)
            await asyncio.sleep(self.check_interval)
