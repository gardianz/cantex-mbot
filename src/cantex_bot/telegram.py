"""Telegram logging via the Bot API (aiohttp, no heavy dependency)."""
from __future__ import annotations

import logging

import aiohttp

from .config import TelegramConfig

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Fire-and-forget Telegram sender. Never raises to the caller."""

    # Disable after this many consecutive failures (e.g. a wrong bot token
    # returning 404) so the logs are not flooded.
    _MAX_FAILS = 5

    def __init__(self, config: TelegramConfig) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._fails = 0
        self._disabled = False

    @property
    def enabled(self) -> bool:
        return self.config.usable and not self._disabled

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def send(self, text: str) -> None:
        if not self.enabled:
            logger.debug("Telegram disabled, dropping message: %s", text)
            return
        url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
        payload = {
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    self._note_failure(f"HTTP {resp.status}: {body[:120]}")
                else:
                    self._fails = 0
        except Exception as exc:  # noqa: BLE001 - notifications must not break the bot
            self._note_failure(str(exc))

    def _note_failure(self, why: str) -> None:
        self._fails += 1
        if self._fails >= self._MAX_FAILS:
            self._disabled = True
            logger.warning(
                "Telegram disabled after %d consecutive failures (last: %s). "
                "Check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.", self._fails, why,
            )
        else:
            logger.warning("Telegram send failed (%d/%d): %s",
                           self._fails, self._MAX_FAILS, why)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
