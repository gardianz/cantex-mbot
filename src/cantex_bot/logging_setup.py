"""Logging configuration.

The CLI is an interactive TUI (questionary menus + a live rich dashboard), so
log records must NOT go to the console — a stray log line corrupts the menu
render. All logs go to a file instead; user-facing output uses ``console.print``.
Tail the file to watch logs live:  ``tail -f cantex_bot.log``.

A small in-memory ring buffer also keeps the most recent records so the live
dashboard can show a log panel. Read it with ``recent_logs()``.
"""
from __future__ import annotations

import logging
from collections import deque

_RING: deque[str] = deque(maxlen=200)


class _RingHandler(logging.Handler):
    """Keep the last N formatted records in memory for the dashboard panel."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _RING.append(self.format(record))
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


def recent_logs(n: int = 8) -> list[str]:
    """The most recent ``n`` log lines (oldest first)."""
    if n <= 0:
        return []
    return list(_RING)[-n:]


class _RetryNoiseFilter(logging.Filter):
    """Drop the SDK's per-attempt retry WARNINGs (``... failed (attempt 1/4)``).

    These are transient WSL2 connection stalls the SDK recovers from on a later
    attempt; the outcome that matters is still logged — success as INFO, or a
    final give-up as ERROR — plus PortfolioService logs its own WARNING if a
    whole refresh fails. So the intermediate attempt chatter is pure noise that
    otherwise floods the dashboard LOG panel after a burst of swaps.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if (record.levelno == logging.WARNING
                and record.name == "cantex_sdk._sdk"
                and "failed (attempt" in record.getMessage()):
            return False
        return True


def setup(level: int = logging.INFO, logfile: str = "cantex_bot.log") -> None:
    noise = _RetryNoiseFilter()
    handlers: list[logging.Handler] = []
    try:
        fh = logging.FileHandler(logfile)
        fh.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
        )
        fh.addFilter(noise)
        handlers.append(fh)
    except OSError:
        # No writable file — fall back to a null handler so records are dropped,
        # never printed to the console (which would break the interactive menu).
        handlers.append(logging.NullHandler())

    ring = _RingHandler()
    ring.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                                        datefmt="%H:%M:%S"))
    ring.addFilter(noise)
    handlers.append(ring)

    logging.basicConfig(level=level, handlers=handlers, format="%(message)s")
    # aiohttp/asyncio noise down
    logging.getLogger("asyncio").setLevel(logging.WARNING)
