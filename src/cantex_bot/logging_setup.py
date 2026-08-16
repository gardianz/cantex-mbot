"""Logging configuration.

The CLI is an interactive TUI (questionary menus + a live rich dashboard), so
log records must NOT go to the console — a stray log line corrupts the menu
render. All logs go to a file instead; user-facing output uses ``console.print``.
Tail the file to watch logs live:  ``tail -f cantex_bot.log``.

A small in-memory ring buffer also keeps the most recent records so the live
dashboard can show a log panel. Read it with ``recent_logs()``, optionally
filtered to one wallet — see ``wallet_logs`` for how a record is attributed.
"""
from __future__ import annotations

import contextvars
import logging
import os
import re
from collections import deque
from contextlib import contextmanager

# (wallet | None, formatted line). Held long enough that filtering to ONE wallet
# still yields a useful window: with hundreds of wallets interleaved, a 200-line
# buffer can hold only a couple of lines per wallet.
_RING: deque[tuple[str | None, str]] = deque(maxlen=2000)

# The wallet whose work the current task is doing. Set by the per-wallet loops
# (strategy, portfolio sweep, bulk swap/withdraw) so records logged *below* them
# — including the SDK's own, which know nothing about wallets — are attributed
# correctly. asyncio copies the context per task, so concurrent wallets never
# see each other's value.
_current_wallet: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cantex_log_wallet", default=None,
)

# Messages that name their wallet explicitly. Checked before the context var, so
# a line logged from a parent task about one wallet is still attributed to it.
_WALLET_PATTERNS = (
    re.compile(r"^\[([^\]\s]+)\]"),                     # "[w1] buy 10 USDCX->CBTC"
    re.compile(r"^portfolio refresh (\S+) failed"),     # PortfolioService
    re.compile(r"^Wallet (\S+) (?:authenticated|auth failed)"),
    re.compile(r"\bwallet (\S+) crashed"),              # Strategy.run
)


@contextmanager
def wallet_logs(name: str):
    """Attribute every record logged inside this block to ``name``.

    Wrap a per-wallet unit of work with it. Without this, a record from the SDK
    (``cantex_sdk._sdk API GET /v1/account/info returned 502``) carries no wallet
    and cannot be shown in a per-wallet log view — which is exactly the record
    you need when one wallet stalls and the others keep going.
    """
    token = _current_wallet.set(name)
    try:
        yield
    finally:
        _current_wallet.reset(token)


def _wallet_of(record: logging.LogRecord) -> str | None:
    try:
        msg = record.getMessage()
    except Exception:  # noqa: BLE001 - a bad format string must not lose the line
        msg = ""
    for pattern in _WALLET_PATTERNS:
        m = pattern.search(msg)
        if m:
            return m.group(1)
    return _current_wallet.get()


class _RingHandler(logging.Handler):
    """Keep the last N formatted records in memory for the dashboard panel."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _RING.append((_wallet_of(record), self.format(record)))
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


def recent_logs(n: int = 8, wallet: str | None = None) -> list[str]:
    """The most recent ``n`` log lines (oldest first).

    ``wallet`` restricts the result to records attributed to that wallet. Lines
    that belong to no wallet (scheduler, Telegram, menu errors) only appear in
    the unfiltered view.
    """
    if n <= 0:
        return []
    if wallet is None:
        return [line for _, line in _RING][-n:]
    want = wallet.casefold()
    return [line for w, line in _RING if w is not None and w.casefold() == want][-n:]


class _RetryNoiseFilter(logging.Filter):
    """Drop the SDK's per-attempt retry WARNINGs (``... failed (attempt 1/4)``).

    These are transient WSL2 connection stalls the SDK recovers from on a later
    attempt; the outcome that matters is still logged — success as INFO, or a
    final give-up as ERROR — plus PortfolioService logs its own WARNING if a
    whole refresh fails. So the intermediate attempt chatter is pure noise that
    otherwise floods the dashboard LOG panel after a burst of swaps.

    Except when diagnosing timeouts: the dropped line is the ONLY place the
    underlying exception is recorded. ``CantexTimeoutError: ... timed out after
    4 attempts`` cannot tell a connect stall from a slow read from a queued
    connection — the attempt lines can. Set ``CANTEX_LOG_RETRIES=1`` to keep
    them; they are attributed per wallet, so the dashboard's LOG panel shows
    them under the stalling wallet.
    """

    def __init__(self, keep_retries: bool = False) -> None:
        super().__init__()
        self.keep_retries = keep_retries

    def filter(self, record: logging.LogRecord) -> bool:
        if self.keep_retries:
            return True
        if (record.levelno == logging.WARNING
                and record.name == "cantex_sdk._sdk"
                and "failed (attempt" in record.getMessage()):
            return False
        return True


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() not in ("", "0", "false", "no", "off")


def setup(level: int = logging.INFO, logfile: str = "cantex_bot.log") -> None:
    # CANTEX_LOG_RETRIES=1 keeps the SDK's per-attempt lines — the only record of
    # what a "timed out after 4 attempts" actually was.
    noise = _RetryNoiseFilter(keep_retries=_truthy(os.getenv("CANTEX_LOG_RETRIES")))
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
