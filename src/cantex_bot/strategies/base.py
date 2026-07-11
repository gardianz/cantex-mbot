"""Strategy base class."""
from __future__ import annotations

import abc
import asyncio


class Strategy(abc.ABC):
    """A runnable trading strategy. Cooperative-cancellation via a stop event."""

    name: str = "strategy"

    @abc.abstractmethod
    async def run(self, stop: asyncio.Event) -> None:
        """Run until the daily target is met or ``stop`` is set."""
        ...
