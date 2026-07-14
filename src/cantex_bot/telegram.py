"""Telegram logging via the Bot API (aiohttp, no heavy dependency)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import aiohttp

from .config import TelegramConfig

logger = logging.getLogger(__name__)

# Telegram rejects messages over 4096 chars; leave room for the <pre> wrapper.
_MAX_MSG = 3900


class TelegramNotifier:
    """Fire-and-forget Telegram sender. Never raises to the caller."""

    # After this many consecutive failures, PAUSE (not permanently disable) for a
    # cooldown so the logs aren't flooded — then retry, so a transient network
    # blip can't kill Telegram for the whole session.
    _MAX_FAILS = 5
    _COOLDOWN = 60.0

    def __init__(self, config: TelegramConfig) -> None:
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._fails = 0
        self._paused_until = 0.0

    @property
    def enabled(self) -> bool:
        return self.config.usable and time.monotonic() >= self._paused_until

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
            self._paused_until = time.monotonic() + self._COOLDOWN
            self._fails = 0
            logger.warning(
                "Telegram paused %.0fs after repeated failures (last: %s). "
                "Check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.", self._COOLDOWN, why,
            )
        else:
            logger.warning("Telegram send failed (%d/%d): %s",
                           self._fails, self._MAX_FAILS, why)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# A command handler takes the raw argument string (may be empty) and returns the
# reply text (plain — the bot wraps it in a monospace <pre> block).
Handler = Callable[[str], Awaitable[str]]


class TelegramCommandBot:
    """Long-polls ``getUpdates`` and dispatches slash commands (e.g. ``/stats``).

    Security: replies go only to the configured ``chat_id`` and messages from
    any other chat are ignored, so the bot answers just its owner. Runs as a
    background task; ``start``/``stop`` are safe to call when Telegram is
    disabled (they no-op). Never raises to the caller."""

    def __init__(
        self,
        config: TelegramConfig,
        notifier: TelegramNotifier,
        handlers: dict[str, Handler],
        *,
        poll_timeout: int = 25,
    ) -> None:
        self.config = config
        self.notifier = notifier          # reused for outbound sends (same chat)
        self.handlers = handlers
        self.poll_timeout = poll_timeout
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._offset: int | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.usable)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if not self.enabled:
            return
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self.poll_timeout + 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()

    # -- polling -------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Total timeout must outlast the server-side long-poll window.
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.poll_timeout + 15)
            )
        return self._session

    async def _run(self) -> None:
        await self._drain_backlog()
        while not self._stop.is_set():
            try:
                updates = await self._poll()
            except Exception as exc:  # noqa: BLE001 - polling must never die
                if not self._stop.is_set():
                    logger.debug("Telegram poll failed: %s", exc)
                    await asyncio.sleep(3)
                continue
            for upd in updates:
                await self._dispatch(upd)

    async def _drain_backlog(self) -> None:
        """Skip messages that arrived before startup so the bot never replays a
        stale ``/stats`` from a previous run."""
        try:
            updates = await self._get_updates(timeout=0, offset=-1)
        except Exception:  # noqa: BLE001
            return
        if updates:
            self._offset = updates[-1]["update_id"] + 1

    async def _poll(self) -> list[dict]:
        return await self._get_updates(timeout=self.poll_timeout, offset=self._offset)

    async def _get_updates(self, *, timeout: int, offset: int | None) -> list[dict]:
        url = f"https://api.telegram.org/bot{self.config.bot_token}/getUpdates"
        payload: dict = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        session = await self._get_session()
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "getUpdates failed"))
        updates = data.get("result", [])
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    # -- dispatch ------------------------------------------------------------

    async def _dispatch(self, update: dict) -> None:
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        # Only answer the configured chat; ignore everyone else.
        if str(chat.get("id")) != str(self.config.chat_id):
            return
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        head, _, arg = text.partition(" ")
        cmd = head[1:].split("@", 1)[0].lower()  # "/stats@bot" -> "stats"
        handler = self.handlers.get(cmd)
        if handler is None:
            known = ", ".join(f"/{c}" for c in self.handlers) or "(none)"
            await self._reply(f"Unknown command /{cmd}. Try: {known}")
            return
        try:
            reply = await handler(arg.strip())
        except Exception as exc:  # noqa: BLE001 - one bad command must not kill the loop
            logger.warning("Telegram command /%s failed: %s", cmd, exc)
            reply = f"/{cmd} failed: {exc}"
        await self._reply(reply)

    async def _reply(self, text: str) -> None:
        """Send ``text`` back as one or more monospace blocks (chunked to fit)."""
        for chunk in _chunk_lines(text, _MAX_MSG):
            await self.notifier.send(f"<pre>{_html_escape(chunk)}</pre>")


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _chunk_lines(text: str, limit: int) -> list[str]:
    """Split ``text`` into <=limit-char pieces on line boundaries (a single
    over-long line is hard-split)."""
    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        add = line if not buf else buf + "\n" + line
        if len(add) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = add
    if buf:
        chunks.append(buf)
    return chunks or [""]
