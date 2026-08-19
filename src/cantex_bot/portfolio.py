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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .ccview import CCViewClient, fee_windows
from .logging_setup import wallet_logs
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
    base_symbol: str = "USDCX"        # strategy base currency in force
    base_bal: Decimal = Decimal(0)    # balance of the base currency (shown col 1)
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
    # Fee/loss over the periods the REWARDS actually cover — see _reward_windows.
    # These, not the plain today/yesterday/week values, are what profit uses.
    fee_reward_day: Decimal = Decimal(0)
    fee_reward_week: Decimal = Decimal(0)
    loss_reward_day: Decimal = Decimal(0)
    loss_reward_week: Decimal = Decimal(0)
    reward_day: date | None = None  # the day reb_yesterday actually covers
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
    base_bal: Decimal = Decimal(0)
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
        # Per-pair fee stats, recomputed once per sweep (off the render path so
        # the dashboard never runs the heavy GROUP BY on the event loop).
        self.pair_fees: list = []
        # CC price (base-token per 1 CC) for converting base-denominated loss to
        # CC — market-wide, cached briefly, keyed by the base symbol in force.
        self._market = None
        self._cc_price = Decimal(0)
        self._cc_price_base = ""
        self._cc_price_ts = 0.0
        self._cc_price_ttl = 60.0
        # Periodically quote base->every pool token so the dashboard's PAIR FEES
        # panel lists ALL pairs, not just the ones actually swapped.
        self.fee_probe_interval = 90.0
        self._last_fee_probe = 0.0

    # -- one wallet ----------------------------------------------------------

    def _focus_tokens(self) -> list[str]:
        """Selected pair tokens for the TOKEN column (CC has its own column and
        is excluded here to avoid duplication)."""
        if self.run_state is not None and self.run_state.selected_tokens:
            return [t for t in self.run_state.selected_tokens
                    if t.upper() != self.cc_symbol]
        return []

    async def refresh_wallet(self, name: str) -> None:
        """Refresh one wallet, attributing every record below it (including the
        SDK's own connection warnings) to that wallet for the dashboard's
        per-wallet log view."""
        with wallet_logs(name):
            await self._refresh_wallet(name)

    async def _refresh_wallet(self, name: str) -> None:
        snap = self.snaps[name]
        wallet = self.manager.get(name)
        async with self.manager.sem:
            try:
                await wallet.ensure_auth()
                info = await wallet.sdk.get_account_info()
                base = self._active_base()
                snap.usdcx = self._balance(info, self.usdcx_symbol)
                snap.base_symbol = base
                snap.base_bal = self._balance(info, base)
                snap.cc = self._balance(info, self.cc_symbol)
                snap.tokens = [(sym, self._balance(info, sym)) for sym in self._focus_tokens()]
                reward_windows = None
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
                    snap.reward_day = reb.yesterday_date
                    # Loss over the exact periods the rewards cover.
                    day_w, week_w = self._reward_windows(reb)
                    snap.loss_reward_day = self._to_cc(WebClient.loss_between(
                        trades, usdcx_symbol=base,
                        start=day_w[0], end=day_w[1]), cc_price)
                    snap.loss_reward_week = self._to_cc(WebClient.loss_between(
                        trades, usdcx_symbol=base,
                        start=week_w[0], end=week_w[1]), cc_price)
                    reward_windows = (day_w, week_w)
                # ccview fees: throttled — reuse the last values until fee_ttl.
                now_m = time.monotonic()
                # fee_updated == 0 means "never fetched" — always fetch then, else
                # a process whose monotonic clock is still below fee_ttl (started
                # soon after boot) would show no fees for the first ttl seconds.
                if snap.fee_updated == 0 or now_m - snap.fee_updated >= self.fee_ttl:
                    fw = fee_windows()
                    wanted = {
                        "today": fw["today"],
                        "yesterday": fw["yesterday"],
                        "this_week": fw["this_week"],
                    }
                    if reward_windows is not None:
                        wanted["reward_day"], wanted["reward_week"] = reward_windows
                    # One ccview call per DISTINCT window: when the reward lag is
                    # one day the reward window equals "yesterday" and costs nothing.
                    seen: dict[tuple, Decimal] = {}
                    fees: dict[str, Decimal] = {}
                    for key, win in wanted.items():
                        if win not in seen:
                            seen[win] = (await self.ccview.party_fee(
                                info.address, *win)).fee
                        fees[key] = seen[win]
                    snap.fee_today = fees["today"]
                    snap.fee_yesterday = fees["yesterday"]
                    snap.fee_this_week = fees["this_week"]
                    snap.fee_reward_day = fees.get("reward_day", fees["yesterday"])
                    snap.fee_reward_week = fees.get("reward_week", fees["this_week"])
                    snap.fee_updated = now_m
                snap.fee_now = self.store.latest_fee(name)
                snap.fee_min_today, snap.fee_avg_today, _ = self.store.fee_stats_today(name)
                # Profit = rebates - (fee + loss), in CC, over the period the
                # REWARD covers — not today/yesterday/this-week as we would
                # compute them. Rewards run ~2 days behind, so pairing them with
                # a self-computed window charges costs against a reward that was
                # never for them and drags profit negative.
                snap.profit_yesterday = snap.reb_yesterday - (
                    snap.fee_reward_day + snap.loss_reward_day)
                snap.profit_week = snap.reb_this_week - (
                    snap.fee_reward_week + snap.loss_reward_week)
                snap.status = "ok"
                snap.error = None
                snap.updated = time.monotonic()
            except Exception as exc:  # noqa: BLE001 - per-wallet isolation
                snap.status = "error"
                snap.error = str(exc)
                logger.warning("portfolio refresh %s failed: %s", name, exc)

    @staticmethod
    def _reward_windows(reb) -> tuple[tuple[date, date], tuple[date, date]]:
        """(day_window, week_window) that the two reward figures actually cover.

        Taking the API's dates rather than computing our own keeps this correct
        whatever the reward lag turns out to be — and the lag is easy to misjudge,
        since every date here is UTC while a local clock can already be on the
        next day.

        The day window is the API's own ``date``. The week window runs from the
        API's ``start_datetime`` to that same day, because the week's reward has
        only accrued as far as the last computed day: charging it for costs
        incurred since is what made PROFIT w read negative while the account was
        in fact ahead. Both fall back to the self-computed windows when the API
        omits the dates.
        """
        fw = fee_windows()
        day = reb.yesterday_date
        if day is None:
            return fw["yesterday"], fw["this_week"]
        week_start = reb.this_week_start or fw["this_week"][0]
        # A reward day before the week started (early Monday, or a stale lag)
        # would invert the range; clamp so the week never runs backwards.
        week_end = max(day, week_start)
        return (day, day), (week_start, week_end)

    @staticmethod
    def _balance(info, symbol: str) -> Decimal:
        want = symbol.upper()
        for tok in getattr(info, "tokens", []) or []:
            if (tok.instrument_symbol or tok.instrument.id).upper() == want:
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
        await self._maybe_probe_fees()
        # Recompute per-pair fee stats off the event loop (the query GROUPs over a
        # large fee_obs table). The dashboard reads this cache, never the DB.
        try:
            self.pair_fees = await asyncio.to_thread(self.store.pair_fee_stats)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pair_fee_stats failed: %s", exc)

    async def _maybe_probe_fees(self) -> None:
        """Quote base->every pool token (from one authed wallet) so PAIR FEES
        covers all pairs. Throttled by ``fee_probe_interval``; best-effort."""
        now = time.monotonic()
        if now - self._last_fee_probe < self.fee_probe_interval:
            return
        if self._market is None or self._cc_price <= 0:
            return  # no market/price yet (first sweeps) — nothing to size a quote
        base_symbol = self._active_base()
        wallet = next((self.manager.get(n) for n in self.manager.names
                       if getattr(self.manager.get(n), "authed", False)), None)
        if wallet is None:
            return
        try:
            base = self._market.instrument(base_symbol)
            pairs = self._market.trade_pairs(base_symbol, exclude_symbols=(self.cc_symbol,))
        except Exception:  # noqa: BLE001
            return
        notional = self._cc_price * Decimal(10)  # ~10 CC worth in the base
        self._last_fee_probe = now
        for pair in pairs:
            try:
                async with self.manager.sem:
                    q = await wallet.sdk.get_swap_quote(notional, base, pair.token)
            except Exception:  # noqa: BLE001 - a probe failure must not break the sweep
                continue
            from .guards import SwapGuard
            net, slip, pool = SwapGuard.quote_metrics(q)
            self.store.record_fee(
                wallet.name, f"{base_symbol}->{pair.token_symbol}", net, slip, pool)

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
            t.base_bal += s.base_bal
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
