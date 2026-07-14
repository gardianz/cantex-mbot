"""Background portfolio cache for the dashboard.

At scale (hundreds of wallets) the dashboard must never fetch inline — a render
would fire thousands of requests. Instead this service keeps a per-wallet
``WalletSnap`` and refreshes them in a background loop, bounded by the manager's
global semaphore. The dashboard paints from the cache instantly; a row shows
``loading`` until its first refresh lands.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .ccview import CCViewClient, fee_windows
from .runstate import RunState
from .store import Store
from .wallets import WalletManager
from .webclient import WebClient

logger = logging.getLogger(__name__)

_DAY = 86400
_WEEK = 7 * _DAY


@dataclass
class WalletSnap:
    name: str
    status: str = "loading"          # loading | ok | error
    error: str | None = None
    usdcx: Decimal = Decimal(0)
    cc: Decimal = Decimal(0)          # always-shown reward token (Amulet)
    # Balances of the strategy's selected pair tokens: [(symbol, amount), ...].
    tokens: list[tuple[str, Decimal]] = field(default_factory=list)
    swaps_today: int = 0
    swaps_24h: int = 0
    swaps_7d: int = 0
    # Loss = USDCX-in minus USDCX-back over complete cycles, converted to CC (so
    # it shares the reward/fee unit and feeds the profit formula).
    loss_today: Decimal = Decimal(0)      # CC lost over today's complete cycles
    loss_yesterday: Decimal = Decimal(0)  # CC lost over yesterday's cycles
    loss_week: Decimal = Decimal(0)       # CC lost over this week's cycles
    # Profit = rebates - (fee + loss), all in CC.
    profit_yesterday: Decimal = Decimal(0)
    profit_week: Decimal = Decimal(0)
    fee_now: Decimal | None = None    # latest live quote network fee (CC)
    fee_min_today: Decimal | None = None
    fee_avg_today: Decimal | None = None
    fee_today: Decimal = Decimal(0)   # CC paid out today (ccview)
    fee_yesterday: Decimal = Decimal(0)
    fee_this_week: Decimal = Decimal(0)
    fee_updated: float = 0.0          # monotonic time of last ccview fee refresh
    reb_yesterday: Decimal = Decimal(0)
    reb_this_week: Decimal = Decimal(0)
    reb_last_week: Decimal = Decimal(0)
    reb_status: str = ""
    updated: float = 0.0             # monotonic time of last successful refresh


@dataclass
class Totals:
    wallets: int = 0
    ok: int = 0
    err: int = 0
    loading: int = 0
    usdcx: Decimal = Decimal(0)
    cc: Decimal = Decimal(0)
    swaps_today: int = 0
    swaps_24h: int = 0
    swaps_7d: int = 0
    loss_today: Decimal = Decimal(0)
    loss_yesterday: Decimal = Decimal(0)
    loss_week: Decimal = Decimal(0)
    profit_yesterday: Decimal = Decimal(0)
    profit_week: Decimal = Decimal(0)
    fee_today: Decimal = Decimal(0)
    fee_yesterday: Decimal = Decimal(0)
    fee_this_week: Decimal = Decimal(0)
    fee_min_today: Decimal | None = None
    fee_avg_today: Decimal | None = None
    reb_yesterday: Decimal = Decimal(0)
    reb_this_week: Decimal = Decimal(0)
    reb_last_week: Decimal = Decimal(0)


class PortfolioService:
    def __init__(
        self,
        manager: WalletManager,
        store: Store,
        ccview: CCViewClient,
        *,
        usdcx_symbol: str = "USDCX",
        cc_symbol: str = "CC",
        interval: float = 30.0,
        run_state: RunState | None = None,
        fee_ttl: float = 300.0,
    ) -> None:
        self.manager = manager
        self.store = store
        self.ccview = ccview
        self.usdcx_symbol = usdcx_symbol.upper()
        self.cc_symbol = cc_symbol.upper()
        self.interval = interval
        self.run_state = run_state
        # ccview fees change slowly; refetch at most once per fee_ttl seconds so a
        # 30s sweep over hundreds of wallets does not storm ccview (HTTP 429).
        self.fee_ttl = fee_ttl
        self.snaps: dict[str, WalletSnap] = {
            n: WalletSnap(name=n) for n in manager.names
        }
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._sweeps = 0
        # CC price (base-token per 1 CC) for converting base-denominated loss to
        # CC — market-wide, cached briefly, keyed by the base symbol in force.
        self._market = None
        self._cc_price = Decimal(0)
        self._cc_price_base = ""
        self._cc_price_ts = 0.0
        self._cc_price_ttl = 60.0

    # -- one wallet ----------------------------------------------------------

    def _focus_tokens(self) -> list[str]:
        """Selected pair tokens for the TOKEN column (CC has its own column and
        is excluded here to avoid duplication)."""
        if self.run_state is not None and self.run_state.selected_tokens:
            return [t for t in self.run_state.selected_tokens
                    if t.upper() != self.cc_symbol]
        return []

    async def refresh_wallet(self, name: str) -> None:
        snap = self.snaps[name]
        wallet = self.manager.get(name)
        async with self.manager.sem:
            try:
                await wallet.ensure_auth()
                info = await wallet.sdk.get_account_info()
                snap.usdcx = self._balance(info, self.usdcx_symbol)
                snap.cc = self._balance(info, self.cc_symbol)
                snap.tokens = [(sym, self._balance(info, sym)) for sym in self._focus_tokens()]
                if wallet.web is not None:
                    trades = await wallet.web.fetch_trading_history()
                    snap.swaps_today = WebClient.count_today(trades)
                    snap.swaps_24h = WebClient.count_since(trades, _DAY)
                    snap.swaps_7d = WebClient.count_since(trades, _WEEK)
                    # Loss over each window in the active base currency, then
                    # converted to CC (base = USDCX unless a strategy set another).
                    base = self._active_base()
                    cc_price = await self._ensure_cc_price(wallet, base)
                    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
                    snap.loss_today = self._to_cc(WebClient.daily_loss(
                        trades, usdcx_symbol=base), cc_price)
                    snap.loss_yesterday = self._to_cc(WebClient.daily_loss(
                        trades, usdcx_symbol=base, day=yesterday), cc_price)
                    snap.loss_week = self._to_cc(WebClient.weekly_loss(
                        trades, usdcx_symbol=base), cc_price)
                    reb = await wallet.web.fetch_rebates()
                    snap.reb_yesterday = reb.yesterday
                    snap.reb_this_week = reb.this_week
                    snap.reb_last_week = reb.last_week
                    snap.reb_status = reb.last_week_status
                # ccview fees: throttled — reuse the last values until fee_ttl.
                now_m = time.monotonic()
                if now_m - snap.fee_updated >= self.fee_ttl:
                    fw = fee_windows()
                    snap.fee_today = (await self.ccview.party_fee(info.address, *fw["today"])).fee
                    snap.fee_yesterday = (await self.ccview.party_fee(info.address, *fw["yesterday"])).fee
                    snap.fee_this_week = (await self.ccview.party_fee(info.address, *fw["this_week"])).fee
                    snap.fee_updated = now_m
                snap.fee_now = self.store.latest_fee(name)
                snap.fee_min_today, snap.fee_avg_today, _ = self.store.fee_stats_today(name)
                # Profit = rebates - (fee + loss), in CC, per window.
                snap.profit_yesterday = snap.reb_yesterday - (snap.fee_yesterday + snap.loss_yesterday)
                snap.profit_week = snap.reb_this_week - (snap.fee_this_week + snap.loss_week)
                snap.status = "ok"
                snap.error = None
                snap.updated = time.monotonic()
            except Exception as exc:  # noqa: BLE001 - per-wallet isolation
                snap.status = "error"
                snap.error = str(exc)
                logger.warning("portfolio refresh %s failed: %s", name, exc)

    @staticmethod
    def _balance(info, symbol: str) -> Decimal:
        for tok in getattr(info, "tokens", []) or []:
            if (tok.instrument_symbol or tok.instrument.id).upper() == symbol:
                return tok.unlocked_amount
        return Decimal(0)

    def _active_base(self) -> str:
        """The base currency loss is measured in: the running/last strategy's
        base token, else USDCX."""
        if self.run_state is not None and getattr(self.run_state, "base_symbol", None):
            return self.run_state.base_symbol.upper()
        return self.usdcx_symbol

    async def _ensure_cc_price(self, wallet, base_symbol: str) -> Decimal:
        """``base_symbol`` per 1 CC via a pricing quote, cached market-wide and
        keyed by base. 0 if it can't be priced (loss then shows 0 CC until the
        next successful quote)."""
        base_symbol = base_symbol.upper()
        now = time.monotonic()
        if (self._cc_price_base == base_symbol and self._cc_price > 0
                and now - self._cc_price_ts < self._cc_price_ttl):
            return self._cc_price
        try:
            if self._market is None:
                from .markets import MarketMap
                self._market = await MarketMap.build(wallet.sdk)
            cc = self._market.instrument(self.cc_symbol)
            base = self._market.instrument(base_symbol)
            if base == cc:                       # base IS CC: 1 CC = 1 CC
                self._cc_price, self._cc_price_base, self._cc_price_ts = (
                    Decimal(1), base_symbol, now)
                return self._cc_price
            probe = Decimal(10)  # a small-but-non-dust ticket
            q = await wallet.sdk.get_swap_quote(probe, cc, base)
            price = q.returned_amount / probe    # base per 1 CC
            if price > 0:
                self._cc_price, self._cc_price_base, self._cc_price_ts = (
                    price, base_symbol, now)
        except Exception as exc:  # noqa: BLE001 - pricing is best-effort
            logger.debug("cc price quote (base %s) failed: %s", base_symbol, exc)
        # Never return a price cached for a DIFFERENT base.
        return self._cc_price if self._cc_price_base == base_symbol else Decimal(0)

    @staticmethod
    def _to_cc(usdcx_amount: Decimal, cc_price: Decimal) -> Decimal:
        """Convert a USDCX amount to CC given the USDCX-per-CC price."""
        return usdcx_amount / cc_price if cc_price > 0 else Decimal(0)

    # -- sweep + loop --------------------------------------------------------

    async def refresh_all(self) -> None:
        # gather all; the manager semaphore bounds real concurrency.
        await asyncio.gather(
            *(self.refresh_wallet(n) for n in self.manager.names),
            return_exceptions=True,
        )
        self._sweeps += 1

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.refresh_all()
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
            with_suppress = self._task
            try:
                await asyncio.wait_for(with_suppress, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                with_suppress.cancel()
            self._task = None

    async def refresh_once(self) -> None:
        """A single bounded sweep (for the snapshot/print path)."""
        await self.refresh_all()

    # -- aggregate -----------------------------------------------------------

    def totals(self) -> Totals:
        t = Totals(wallets=len(self.snaps))
        avgs: list[Decimal] = []
        for s in self.snaps.values():
            if s.status == "ok":
                t.ok += 1
            elif s.status == "error":
                t.err += 1
            else:
                t.loading += 1
            t.usdcx += s.usdcx
            t.cc += s.cc
            t.swaps_today += s.swaps_today
            t.swaps_24h += s.swaps_24h
            t.swaps_7d += s.swaps_7d
            t.loss_today += s.loss_today
            t.loss_yesterday += s.loss_yesterday
            t.loss_week += s.loss_week
            t.profit_yesterday += s.profit_yesterday
            t.profit_week += s.profit_week
            t.fee_today += s.fee_today
            t.fee_yesterday += s.fee_yesterday
            t.fee_this_week += s.fee_this_week
            t.reb_yesterday += s.reb_yesterday
            t.reb_this_week += s.reb_this_week
            t.reb_last_week += s.reb_last_week
            if s.fee_min_today is not None:
                t.fee_min_today = (
                    s.fee_min_today if t.fee_min_today is None
                    else min(t.fee_min_today, s.fee_min_today)
                )
            if s.fee_avg_today is not None:
                avgs.append(s.fee_avg_today)
        if avgs:
            t.fee_avg_today = sum(avgs) / Decimal(len(avgs))
        return t
