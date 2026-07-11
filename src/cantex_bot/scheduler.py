"""Daily scheduler for strategies.

Daily counters are keyed by calendar date in the store, so a new day resets
naturally. This just re-runs the strategy each day and sleeps in between.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .strategies.base import Strategy
from .telegram import TelegramNotifier

logger = logging.getLogger(__name__)


def _seconds_until_next_midnight() -> float:
    """Seconds until the next 00:00 UTC — the daily reset boundary the store,
    swap counts and loss windows all use (matches the Cantex web, which is UTC).
    A local-time midnight here would reset hours early/late (e.g. WIB = UTC+7)."""
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (tomorrow - now).total_seconds()


class StrategyScheduler:
    def __init__(self, strategy: Strategy, notifier: TelegramNotifier) -> None:
        self.strategy = strategy
        self.notifier = notifier

    async def run_once(self, stop: asyncio.Event) -> None:
        await self.strategy.run(stop)

    async def run_daily(self, stop: asyncio.Event) -> None:
        """Run the strategy now, then once per calendar day until stopped."""
        while not stop.is_set():
            try:
                await self.strategy.run(stop)
            except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
                logger.exception("Strategy run errored: %s", exc)
                await self.notifier.send(f"❌ Scheduler: strategy errored: {exc}")

            if stop.is_set():
                break
            wait = _seconds_until_next_midnight()
            logger.info("Sleeping %.0fs until next day", wait)
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass  # new day; loop again
