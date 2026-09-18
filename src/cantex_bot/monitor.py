"""Read-only market monitor: quote every pair on a loop, record the metrics.

This never swaps. ``get_swap_quote`` is a pricing call — it costs nothing and
moves nothing — so the monitor can watch the whole market continuously and build
up today's min/max/average without touching a balance.

Both directions are quoted, because they are genuinely different numbers: the
network fee, slippage and pool depth for ``base -> token`` need not match
``token -> base``, and the strategy pays whichever one it is about to cross. The
sell-side amount comes from the buy-side quote's own output, so the pair is
measured as an actual round trip rather than at two unrelated sizes.

The samples land in the same ``fee_obs`` table the trading side writes to, so the
monitor's view and the dashboard's PAIR FEES panel are one dataset.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

from cantex_sdk import CantexError, InstrumentId

from .guards import SwapGuard
from .markets import MarketMap
from .store import PairStats, Store
from .wallets import WalletManager

logger = logging.getLogger(__name__)


@dataclass
class MonitorState:
    """What the monitor dashboard paints. Written by the sweep, read by render."""

    base_symbol: str = ""
    pairs: int = 0                 # distinct tokens being watched
    sweeps: int = 0
    last_sweep: float = 0.0        # monotonic, 0 = none finished yet
    last_duration: float = 0.0     # seconds the last sweep took
    quoted: int = 0                # successful quotes in the last sweep
    failed: int = 0                # quotes that errored in the last sweep
    interval: float = 0.0          # configured gap between sweeps
    notional: Decimal = Decimal(0)  # base amount each buy-side quote uses
    errors: list[str] = field(default_factory=list)   # newest first, capped
    status: str = "starting"


class FeeMonitor:
    """Quote every base<->token pair on a loop. Never submits a swap."""

    MAX_ERRORS = 5

    def __init__(
        self,
        manager: WalletManager,
        store: Store,
        *,
        base_symbol: str,
        cc_symbol: str = "CC",
        cc_units: Decimal = Decimal("110"),
        tokens: list[str] | None = None,
        interval: float = 30.0,
        both_ways: bool = True,
    ) -> None:
        self.manager = manager
        self.store = store
        self.base_symbol = base_symbol.upper()
        self.cc_symbol = cc_symbol.upper()
        self.cc_units = cc_units
        self.tokens = tokens
        self.interval = interval
        # Each pair costs one request per direction. Turning the sell side off
        # halves the sweep when the buy side is all you watch.
        self.both_ways = both_ways
        self.state = MonitorState(base_symbol=self.base_symbol, interval=interval)
        # Recomputed once per sweep, off the event loop. The dashboard paints
        # from here and never touches the DB — render runs on every keypress.
        self.pair_stats: list[PairStats] = []
        self._market: MarketMap | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # -- one sweep -----------------------------------------------------------

    async def _wallet(self):
        """Any configured wallet — quotes are market-wide, not per-account."""
        name = self.manager.names[0]
        wallet = self.manager.get(name)
        await wallet.ensure_auth()
        return wallet

    async def _notional(self, wallet, base: InstrumentId, cc: InstrumentId) -> Decimal:
        """Base value of ``cc_units`` CC — the size the strategy actually trades,
        so the slippage measured here is the slippage it would pay."""
        try:
            quote = await wallet.sdk.get_swap_quote(self.cc_units, cc, base)
            return quote.returned_amount
        except CantexError as exc:
            logger.warning("monitor: could not price CC in %s: %s",
                           self.base_symbol, exc)
            return Decimal(0)

    def _record(self, wallet_name: str, pair: str, quote) -> None:
        net, slip, pool = SwapGuard.quote_metrics(quote)
        size = getattr(getattr(quote, "pool_size", None), "amount", Decimal(0))
        self.store.record_fee(wallet_name, pair, net, slip, pool, size)

    def _note_error(self, message: str) -> None:
        self.state.errors.insert(0, message)
        del self.state.errors[self.MAX_ERRORS:]

    async def sweep_once(self) -> None:
        """Quote every pair, both ways, and record what comes back."""
        started = time.monotonic()
        quoted = failed = 0
        try:
            wallet = await self._wallet()
            if self._market is None:
                self._market = await MarketMap.build(wallet.sdk)
            base = self._market.instrument(self.base_symbol)
            cc = self._market.instrument(self.cc_symbol)
            pairs = self._market.trade_pairs(
                self.base_symbol, only_symbols=self.tokens,
                exclude_symbols=(self.cc_symbol,),
            )
        except Exception as exc:  # noqa: BLE001 - a bad sweep must not kill the loop
            self.state.status = "error"
            self._note_error(f"setup: {exc}")
            logger.warning("monitor sweep setup failed: %s", exc)
            return

        notional = await self._notional(wallet, base, cc)
        if notional <= 0:
            self.state.status = "error"
            self._note_error(f"cannot price {self.cc_units} CC in {self.base_symbol}")
            return
        self.state.notional = notional
        self.state.pairs = len(pairs)
        self.state.status = "running"

        for pair in pairs:
            buy_label = f"{self.base_symbol}->{pair.token_symbol}"
            try:
                async with self.manager.sem:
                    buy = await wallet.sdk.get_swap_quote(notional, base, pair.token)
            except Exception as exc:  # noqa: BLE001 - per-pair isolation
                failed += 1
                self._note_error(f"{buy_label}: {exc}")
                continue
            self._record(wallet.name, buy_label, buy)
            quoted += 1

            # Sell side at the size the buy would actually leave us holding.
            back = buy.returned_amount
            if not self.both_ways or back <= 0:
                continue
            sell_label = f"{pair.token_symbol}->{self.base_symbol}"
            try:
                async with self.manager.sem:
                    sell = await wallet.sdk.get_swap_quote(back, pair.token, base)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self._note_error(f"{sell_label}: {exc}")
                continue
            self._record(wallet.name, sell_label, sell)
            quoted += 1

        self.state.quoted = quoted
        self.state.failed = failed
        self.state.sweeps += 1
        self.state.last_duration = time.monotonic() - started
        self.state.last_sweep = time.monotonic()
        if failed and not quoted:
            self.state.status = "error"
        # The GROUP BY over fee_obs is the expensive part; keep it off the loop.
        try:
            self.pair_stats = await asyncio.to_thread(self.store.pair_fee_stats)
        except Exception as exc:  # noqa: BLE001 - stats are cosmetic
            logger.debug("monitor pair_fee_stats failed: %s", exc)

    # -- loop ----------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.sweep_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            task, self._task = self._task, None
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        self.state.status = "stopped"
