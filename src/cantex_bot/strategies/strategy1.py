"""Strategy 1: cycle USDCX <-> every USDCX pair, back and forth.

Per wallet, per day:
  * Buy side  (USDCX -> token): sized to the market value of N CC tokens.
  * Sell side (token -> USDCX): sells 100% of the token's unlocked balance.
Alternates buy/sell across all USDCX pairs (round-robin) until the wallet hits
its daily swap target. Every swap goes through SwapEngine (guards enforced).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from cantex_sdk import CantexError, InstrumentId

from ..config import Strategy1Config
from ..logging_setup import wallet_logs
from ..markets import MarketMap
from .. import runstate as run_status
from ..runstate import RunState
from ..swapper import SwapEngine
from ..telegram import TelegramNotifier
from ..store import Store
from ..wallets import Wallet, WalletManager
from ..webclient import WebClient, WebClientError
from .base import Strategy

logger = logging.getLogger(__name__)


class Strategy1(Strategy):
    name = "strategy1"
    label = "Strategy1"          # display name in logs / Telegram (subclasses override)

    def __init__(
        self,
        manager: WalletManager,
        engine: SwapEngine,
        config: Strategy1Config,
        notifier: TelegramNotifier,
        store: Store,
        run_state: "RunState | None" = None,
        tokens: list[str] | None = None,
        base_symbol: str | None = None,
    ) -> None:
        self.manager = manager
        self.engine = engine
        self.config = config
        self.notifier = notifier
        self.store = store
        self.run_state = run_state
        # Base token to cycle against (default USDCX). Any pool token works —
        # the swap endpoint routes token->token multi-hop via CC.
        self.base_symbol = (base_symbol or config.usdcx_symbol).upper()
        # Token symbols to trade against the base. None => every pool token
        # (minus the base and CC). Chosen interactively in the CLI.
        self.tokens = tokens
        # Per-wallet cache of (monotonic_ts, web_swaps_today) for the poll loop.
        self._web_cache: dict[str, tuple[float, int]] = {}
        # Per-wallet cache of (monotonic_ts, loss_today_in_base) for the budget.
        self._loss_cache: dict[str, tuple[float, Decimal]] = {}
        # When a sell was first held back by the cycle-loss brake: (wallet, token).
        self._held_since: dict[tuple[str, str], float] = {}

    def _pairs_for(self, market: MarketMap):
        """base<->token pairs to trade, honouring the chosen token subset."""
        return market.trade_pairs(
            self.base_symbol,
            only_symbols=self.tokens,
            exclude_symbols=(self.config.cc_symbol,),
        )

    async def run(self, stop: asyncio.Event) -> None:
        await self.notifier.send(
            f"▶️ {self.label} start (base {self.base_symbol}, "
            f"target {self.config.daily_swap_target}/wallet, "
            f"cc_units={self.config.cc_units}, dry_run={self.engine.dry_run})"
        )
        if self.run_state is not None:
            selected = await self._selected_tokens()
            self.run_state.begin(self.manager.names, selected,
                                 base_symbol=self.base_symbol)
        try:
            results = await asyncio.gather(
                *(self._run_wallet(w, stop) for w in self.manager.wallets.values()),
                return_exceptions=True,
            )
        finally:
            if self.run_state is not None:
                self.run_state.end()
        for name, res in zip(self.manager.names, results):
            if isinstance(res, Exception):
                logger.error("%s wallet %s crashed: %s", self.label, name, res)
                if self.run_state is not None:
                    self.run_state.finish(name, status=run_status.ERROR)
                await self.notifier.send(f"❌ {self.label} [{name}] crashed: {res}")
        await self.notifier.send(f"⏹️ {self.label} finished")

    async def _selected_tokens(self) -> list[str]:
        """Token symbols the strategy will trade this run."""
        try:
            wallet = next(iter(self.manager.wallets.values()))
            await wallet.ensure_auth()
            market = await MarketMap.build(wallet.sdk)
            return [p.token_symbol for p in self._pairs_for(market)]
        except Exception:  # noqa: BLE001
            return list(self.tokens or [])

    def _st(self, name: str, **kw) -> None:
        if self.run_state is not None:
            self.run_state.set(name, **kw)

    @staticmethod
    def _is_too_small(error: str) -> bool:
        """True if a swap error is the exchange's minimum-ticket / dust rejection."""
        e = error.lower()
        return "too small" in e or "minimum ticket" in e or "min ticket" in e

    async def _run_wallet(self, wallet: Wallet, stop: asyncio.Event) -> None:
        """Run one wallet's loop with every record below it — this module's, the
        engine's, and the SDK's — attributed to that wallet, so the dashboard can
        show one wallet's log in isolation when it stalls."""
        with wallet_logs(wallet.name):
            await self._trade_wallet(wallet, stop)

    async def _trade_wallet(self, wallet: Wallet, stop: asyncio.Event) -> None:
        await wallet.ensure_auth()
        market = await MarketMap.build(wallet.sdk)
        usdcx = market.instrument(self.base_symbol)
        cc = market.instrument(self.config.cc_symbol)
        pairs = self._pairs_for(market)
        if not pairs:
            logger.warning("[%s] no tradeable pairs (tokens=%s)", wallet.name, self.tokens)
            self._st(wallet.name, status=run_status.STOPPED)
            await self.notifier.send(
                f"⚠️ {self.label} [{wallet.name}] no tradeable pairs "
                f"(selected: {self.tokens or 'all'})"
            )
            return
        self._st(wallet.name, target=self.config.daily_swap_target,
                 status=run_status.RUNNING)

        buy_notional = await self._price_cc_in_usdcx(wallet, cc, usdcx)
        if buy_notional <= 0:
            logger.error("[%s] could not price CC, aborting wallet", wallet.name)
            return
        logger.info(
            "[%s] buy notional = %s USDCX (= %s CC), %d pairs",
            wallet.name, buy_notional, self.config.cc_units, len(pairs),
        )

        target = self.config.daily_swap_target
        max_consecutive_fail = max(6, len(pairs) * 2)
        consecutive_fail = 0
        session_executed = 0
        # Per-wallet selection state for `_pick` (round-robin index + buy size).
        state = {"idx": 0, "notional": buy_notional}

        # Daily target counts successful swaps from the web trading history.
        prior_web = await self._web_swaps_today(wallet)
        logger.info("[%s] web swaps already today: %d", wallet.name, prior_web)

        insufficient_streak = 0
        usym = self.base_symbol
        run_day = datetime.now(timezone.utc).date()
        while not stop.is_set():
            # UTC day rollover: the daily swap target resets at 00:00 UTC (Cantex
            # is UTC). Zero this run's per-day progress — session count AND the
            # web baseline — otherwise `done` carries yesterday's swaps forward.
            today = datetime.now(timezone.utc).date()
            if today != run_day:
                run_day = today
                session_executed = 0
                consecutive_fail = 0
                insufficient_streak = 0
                state["idx"] = 0
                self._web_cache.pop(wallet.name, None)
                self._loss_cache.pop(wallet.name, None)
                prior_web = await self._web_swaps_today(wallet)
                self._st(wallet.name, status=run_status.RUNNING,
                         route="", plan="new day", done=0)
                logger.info("[%s] new UTC day — daily target reset", wallet.name)
                await self.notifier.send(
                    f"🔄 {self.label} [{wallet.name}] new UTC day — target reset")

            done = await self._current_done(wallet, prior_web, session_executed)
            self._st(wallet.name, done=done)
            if consecutive_fail >= max_consecutive_fail:
                logger.error("[%s] aborting: %d consecutive failures",
                             wallet.name, consecutive_fail)
                self._st(wallet.name, status=run_status.STOPPED,
                         plan="stopped: repeated errors")
                break
            # Target hit / out of balance: don't return (a returning wallet would
            # not restart until EVERY wallet returns, and a fee-polling wallet may
            # never do). Instead idle until the next UTC day, when the rollover
            # block above resets the counters and trading resumes.
            if done >= target:
                self._st(wallet.name, status=run_status.DONE,
                         route="", plan="target reached", done=done)
                if not await self._wait_next_day(stop):
                    break
                continue
            # Daily loss budget: once today's realised loss reaches the cap, stop
            # trading this wallet until the next UTC day (the rollover resets it).
            # Measured in CC, the same unit as the dashboard's LOSS column.
            budget = self.config.max_daily_loss_cc
            if budget > 0:
                loss_cc = self._to_cc(
                    await self._daily_loss_base(wallet), state["notional"])
                if loss_cc >= budget:
                    logger.warning("[%s] daily loss %s CC >= budget %s CC — idle "
                                   "until next UTC day", wallet.name, loss_cc, budget)
                    self._st(wallet.name, status=run_status.STOPPED, route="",
                             plan=f"loss limit {loss_cc:.2f} CC")
                    await self.notifier.send(
                        f"🛑 {self.label} [{wallet.name}] daily loss {loss_cc:.2f} CC "
                        f">= budget {budget} CC — paused for today")
                    if not await self._wait_next_day(stop):
                        break
                    continue
            if insufficient_streak >= self.config.insufficient_retries:
                logger.warning("[%s] insufficient balance after %d retries — "
                               "idle until next UTC day", wallet.name, insufficient_streak)
                self._st(wallet.name, status=run_status.STOPPED,
                         route="", plan="saldo kurang")
                if not await self._wait_next_day(stop):
                    break
                continue

            pair, sellable, token_bal, usdcx_bal, cc_bal = await self._pick(
                wallet, pairs, usdcx, cc, state)
            tok = pair.token_symbol

            # ROUTE (shown in its own dashboard column) vs STATUS (the phase).
            if sellable:
                step, route = "sell", f"sell {tok}→{usym}"
            else:
                step, route = "buy", f"buy {usym}→{tok}"

            if step == "buy" and usdcx_bal < buy_notional:
                insufficient_streak += 1
                self._st(wallet.name, status=run_status.WAITING,
                         route=route, plan="saldo kurang")
                logger.info("[%s] insufficient USDCX (%s < %s), skip %s",
                            wallet.name, usdcx_bal, buy_notional, tok)
                await asyncio.sleep(self.config.cooldown_seconds)
                continue

            # Cycle-loss brake: the per-leg guards cannot see a round trip, so a
            # sell-back at a bad price would still execute. Hold the sell while it
            # would lose more than max_cycle_loss_pct of what the buy cost, and let
            # it through once the price recovers (or the hold times out, so a
            # wallet is never stuck in a token forever).
            cap = self.config.max_cycle_loss_pct
            min_profit = self.config.min_profit_pct_override_fee
            take_profit = False
            force_sl = False
            loss_pct = None
            if step == "sell" and (cap > 0 or min_profit > 0):
                loss_pct = await self._cycle_loss_pct(
                    wallet, pair.token, tok, token_bal, usdcx)
                # Profitable enough to stop waiting out the network fee? The fee
                # is a fraction of the gain, so holding risks the gain for nothing.
                if loss_pct is not None and min_profit > 0 and -loss_pct >= min_profit:
                    take_profit = True
                    logger.info("[%s] taking profit on %s: round trip +%.2f%% "
                                "(>= %s%%) — waiving the network-fee limit",
                                wallet.name, tok, -loss_pct, min_profit)
                if loss_pct is not None and cap > 0 and loss_pct > cap:
                    if not self._hold_expired(wallet.name, tok):
                        self._st(wallet.name, status=run_status.WAITING, route=route,
                                 # Word it, don't sign it: the LOSS column reads
                                 # minus as a GAIN, so a signed number here would
                                 # mean the opposite of the same sign there.
                                 plan=f"tunggu rugi {loss_pct:.2f}%")
                        logger.info("[%s] holding %s: round trip would lose %.2f%% "
                                    "(> %s%%)", wallet.name, tok, loss_pct, cap)
                        await asyncio.sleep(self._poll_interval(
                            SimpleNamespace(guard=None)))
                        continue
                    # Timed stop-loss: stop waiting for the price and sell — but
                    # still under the fee guard. The hold timer is NOT reset here:
                    # if the fee guard rejects the sell, the stop stays armed and
                    # retries every poll, so it fires the moment the fee allows
                    # (resetting it here restarted the wait and the stop could
                    # never fire while the fee sat above the limit).
                    force_sl = True
                    logger.warning("[%s] %s held too long (loss %.2f%%) — stop-loss "
                                   "armed, selling as soon as the fee allows",
                                   wallet.name, tok, loss_pct)
                else:
                    # Not holding any more (price recovered / measurable again).
                    self._clear_hold(wallet.name, tok)

            # Snapshot the web swap count so an ambiguous outcome (below) can be
            # reconciled against the trading history.
            pre_web = await self._web_swaps_today(wallet)
            self._st(wallet.name, status=run_status.SWAPPING, route=route,
                     plan=(f"ambil profit {-loss_pct:.2f}%" if take_profit
                           else f"stop loss {loss_pct:.2f}%" if force_sl
                           else "proses swap"))
            if sellable:
                out = await self.engine.execute_swap(
                    wallet, sell=pair.token, buy=usdcx, sell_amount=token_bal,
                    sell_symbol=tok, buy_symbol=usym,
                    direction="sell", quiet_reject=True,
                    # A stop-loss does NOT waive the fee limit — only a clearly
                    # profitable exit does.
                    ignore_network_fee=take_profit,
                )
            else:
                out = await self.engine.execute_swap(
                    wallet, sell=usdcx, buy=pair.token, sell_amount=buy_notional,
                    sell_symbol=usym, buy_symbol=tok,
                    direction="buy", quiet_reject=True,
                )

            # Cannot proceed = a balance problem, NOT a failure to retry forever:
            #  * a "Too small / min ticket" API error (dust that slipped through), or
            #  * the wallet cannot afford the network fee (CC balance < fee).
            # w1 (has CC) keeps polling for a better fee; w2 (no CC) stops.
            fee = out.guard.details.get("network_fee") if out.guard else None
            cant_afford = fee is not None and cc_bal < fee
            if (out.error and self._is_too_small(out.error)) or cant_afford:
                insufficient_streak += 1
                self._st(wallet.name, status=run_status.WAITING,
                         route=route, plan="saldo kurang")
                logger.info("[%s] %s cannot proceed (cc=%s, fee=%s): saldo kurang",
                            wallet.name, step, cc_bal, fee)
                await asyncio.sleep(self.config.cooldown_seconds)
                continue

            # Ambiguous: a live swap was SUBMITTED but confirmation errored — it
            # may still have settled on-chain. NEVER fire the opposite leg on a
            # maybe (that is the buy/sell "collision"): verify against the trading
            # history first — a higher today-count proves the swap went through.
            if (out.error and getattr(out, "submitted_attempt", False)
                    and not self._is_too_small(out.error)):
                self._st(wallet.name, status=run_status.SWAPPING,
                         route=route, plan="pending swap")
                logger.info("[%s] %s confirm errored, checking history…",
                            wallet.name, step)
                if await self._confirm_via_history(wallet, pre_web):
                    insufficient_streak = 0
                    consecutive_fail = 0
                    session_executed += 1
                    if step == "sell":
                        self._clear_hold(wallet.name, tok)
                    self._web_cache.pop(wallet.name, None)
                    self._st(wallet.name, status=run_status.RUNNING,
                             route=route, plan="swap berhasil")
                    await asyncio.sleep(self.config.cooldown_seconds)
                    continue
                consecutive_fail += 1
                self._st(wallet.name, status=run_status.ERROR,
                         route=route, plan="swap gagal")
                logger.warning("[%s] %s not in history — treating as failed",
                               wallet.name, step)
                await asyncio.sleep(self.config.cooldown_seconds)
                continue

            insufficient_streak = 0
            if out.counted:
                session_executed += 1
                if step == "sell":
                    # Position closed — only now may the hold timer restart.
                    self._clear_hold(wallet.name, tok)
                self._web_cache.pop(wallet.name, None)
                self._st(wallet.name, status=run_status.RUNNING,
                         route=route, plan="swap berhasil")
            if out.error:
                consecutive_fail += 1
                self._st(wallet.name, status=run_status.ERROR,
                         route=route, plan="swap gagal")
            elif out.ok:
                consecutive_fail = 0

            # Pace: adaptive poll while waiting on a guard, brief cooldown otherwise.
            if out.reject_reasons and not out.ok:
                if getattr(out, "fee_rejected", False):
                    # The quote passed the guard but the LIVE fee was over the cap
                    # at submit, so the quoted fee would misreport the wait — and
                    # the adaptive poll would read it as "at the limit" and retry
                    # immediately. Back off a full poll_max instead: the live fee
                    # was too high moments ago and every rejected submit costs a
                    # WebSocket round trip.
                    limit = self.engine.guard.config.max_network_fee
                    plan = f"fee naik >{limit}"
                    delay = self.config.poll_max_seconds
                else:
                    plan = f"wait fee {fee:.3f}" if fee is not None else "wait fee"
                    delay = self._poll_interval(out)
                if force_sl:
                    # Stop armed but the fee guard says no — say so, so the row
                    # doesn't look like an ordinary wait.
                    plan = f"SL {plan}"
                self._st(wallet.name, status=run_status.WAITING,
                         route=route, plan=plan)
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(self.config.cooldown_seconds)

        done = await self._current_done(wallet, prior_web, session_executed)
        self._st(wallet.name, done=done)
        if self.run_state is not None:
            self.run_state.finish(
                wallet.name,
                status=run_status.DONE if done >= target else run_status.STOPPED,
            )
        logger.info("[%s] %s done: %d/%d swaps today", wallet.name, self.label, done, target)
        await self.notifier.send(
            f"🏁 {self.label} [{wallet.name}] {done}/{target} swaps today (web-synced)"
        )

    async def _web_swaps_today(self, wallet: Wallet, ttl: float = 15.0) -> int:
        """Successful swaps today (UTC) from web history, 0 if no web.

        Cached per wallet for ``ttl`` seconds so the fee-polling loop does not
        hammer the history endpoint; the cache is invalidated after any swap.
        """
        if wallet.web is None:
            return 0
        now = time.monotonic()
        cached = self._web_cache.get(wallet.name)
        if cached and now - cached[0] < ttl:
            return cached[1]
        try:
            trades = await wallet.web.fetch_trading_history()
            val = WebClient.count_today(trades)
        except WebClientError as exc:
            logger.warning("[%s] web history fetch failed: %s", wallet.name, exc)
            val = cached[1] if cached else 0
        self._web_cache[wallet.name] = (now, val)
        return val

    async def _daily_loss_base(self, wallet: Wallet, ttl: float = 60.0) -> Decimal:
        """Today's realised loss (UTC) in the BASE currency, from the trading
        history. Cached per wallet; 0 when there is no web access."""
        if wallet.web is None:
            return Decimal(0)
        now = time.monotonic()
        cached = self._loss_cache.get(wallet.name)
        if cached and now - cached[0] < ttl:
            return cached[1]
        try:
            trades = await wallet.web.fetch_trading_history()
            val = WebClient.daily_loss(trades, usdcx_symbol=self.base_symbol)
        except WebClientError as exc:
            logger.debug("[%s] loss fetch failed: %s", wallet.name, exc)
            val = cached[1] if cached else Decimal(0)
        self._loss_cache[wallet.name] = (now, val)
        return val

    def _to_cc(self, base_amount: Decimal, notional: Decimal) -> Decimal:
        """Convert a base-currency amount to CC. ``notional`` is what
        ``cc_units`` CC is worth in the base (already quoted for the buy size),
        so ``base per CC = notional / cc_units``. 0 when it can't be priced."""
        units = self.config.cc_units
        if notional <= 0 or units <= 0:
            return Decimal(0)
        return base_amount / (notional / units)

    async def _cycle_loss_pct(
        self, wallet: Wallet, token: InstrumentId, token_symbol: str,
        amount: Decimal, base: InstrumentId,
    ) -> Decimal | None:
        """Loss of the round trip that selling ``amount`` now would close, as a
        percent of the base spent buying it (positive = loss). None when it can't
        be measured (no recorded buy, or the quote failed) — the caller then lets
        the sell through rather than blocking on missing data."""
        spent = self.store.last_buy_cost(wallet.name, self.base_symbol, token_symbol)
        if spent is None or spent <= 0:
            return None
        try:
            q = await wallet.sdk.get_swap_quote(amount, token, base)
        except CantexError:
            return None
        return (spent - q.returned_amount) / spent * Decimal(100)

    def _hold_expired(self, wallet_name: str, token_symbol: str) -> bool:
        """True once a sell has been held back by the cycle-loss brake for longer
        than ``cycle_loss_wait_seconds`` (0 = wait indefinitely). Prevents a
        wallet being stuck in a token forever after a real price move."""
        limit = self.config.cycle_loss_wait_seconds
        key = (wallet_name, token_symbol)
        first = self._held_since.setdefault(key, time.monotonic())
        return bool(limit) and (time.monotonic() - first) >= limit

    def _clear_hold(self, wallet_name: str, token_symbol: str) -> None:
        self._held_since.pop((wallet_name, token_symbol), None)

    async def _confirm_via_history(self, wallet: Wallet, pre_web: int) -> bool:
        """After an ambiguous swap, poll the trading history: a today-count above
        ``pre_web`` means the swap actually settled despite the confirm error.

        Returns False if there is no web history to check (can't confirm → treat
        as not executed, so the same leg is retried rather than flipped)."""
        if wallet.web is None:
            return False
        for _ in range(self.config.confirm_retries):
            await asyncio.sleep(self.config.confirm_interval)
            self._web_cache.pop(wallet.name, None)  # force a fresh fetch
            if await self._web_swaps_today(wallet) > pre_web:
                return True
        return False

    async def _current_done(
        self, wallet: Wallet, prior_web: int, session_executed: int,
    ) -> int:
        """Effective daily swap count: the max of web history, web-at-start plus
        this run's swaps (covers indexing lag), and the local counter."""
        web_now = await self._web_swaps_today(wallet)
        local = self.store.daily_count(wallet.name)
        return max(web_now, prior_web + session_executed, local)

    async def _wait_next_day(self, stop: asyncio.Event) -> bool:
        """Sleep until the next 00:00 UTC (or until stopped). Returns True when a
        new UTC day arrives, False if ``stop`` was set first."""
        now = datetime.now(timezone.utc)
        nxt = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        wait = (nxt - now).total_seconds()
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
            return False
        except asyncio.TimeoutError:
            return True

    def _poll_interval(self, outcome) -> float:
        """Adaptive wait before the next quote: fast when the observed network
        fee is close to the limit, slow when far above it."""
        c = self.config
        lo, hi = c.poll_min_seconds, c.poll_max_seconds
        fee = outcome.guard.details.get("network_fee") if outcome.guard else None
        threshold = self.engine.guard.config.max_network_fee
        if fee is None or threshold <= 0:
            return hi
        gap = (Decimal(str(fee)) - threshold) / threshold  # 0 at limit, >0 above
        if gap <= 0:
            return lo
        far = Decimal(str(c.poll_far_ratio))
        frac = min(gap / far, Decimal(1)) if far > 0 else Decimal(1)
        return float(Decimal(str(lo)) + frac * (Decimal(str(hi)) - Decimal(str(lo))))

    async def _price_cc_in_usdcx(
        self, wallet: Wallet, cc: InstrumentId, usdcx: InstrumentId,
    ) -> Decimal:
        """USDCX value of N CC tokens, via a pricing quote (no swap)."""
        try:
            quote = await wallet.sdk.get_swap_quote(self.config.cc_units, cc, usdcx)
            return quote.returned_amount
        except CantexError as exc:
            logger.error("[%s] CC pricing quote failed: %s", wallet.name, exc)
            return Decimal(0)

    async def _token_balance(self, wallet: Wallet, token: InstrumentId) -> Decimal:
        info = await wallet.sdk.get_account_info()
        return info.get_balance(token)

    async def _balances(
        self, wallet: Wallet, token: InstrumentId, usdcx: InstrumentId,
        cc: InstrumentId,
    ) -> tuple[Decimal, Decimal, Decimal]:
        """(token, usdcx, cc) balances from a single account-info call."""
        info = await wallet.sdk.get_account_info()
        return (info.get_balance(token), info.get_balance(usdcx),
                info.get_balance(cc))

    async def _token_cc_value(
        self, wallet: Wallet, token: InstrumentId, amount: Decimal, cc: InstrumentId,
    ) -> Decimal:
        """CC value of `amount` of `token`, via a pricing quote. 0 on error."""
        try:
            q = await wallet.sdk.get_swap_quote(amount, token, cc)
            return q.returned_amount
        except CantexError:
            return Decimal(0)

    async def _pick(self, wallet, pairs, usdcx, cc, state):
        """Choose the next (pair, sellable) and read balances for it.

        Strategy1: plain round-robin over the pairs. A held token worth at least
        the min ticket is sold; a smaller (dust) amount is ignored so the pair is
        bought instead. Subclasses override this to change target selection (e.g.
        Strategy2 picks the lowest-fee pair). Returns
        ``(pair, sellable, token_bal, usdcx_bal, cc_bal)``."""
        pair = pairs[state["idx"] % len(pairs)]
        state["idx"] += 1
        token_bal, usdcx_bal, cc_bal = await self._balances(
            wallet, pair.token, usdcx, cc)
        sellable = False
        if token_bal > 0:
            cc_value = await self._token_cc_value(wallet, pair.token, token_bal, cc)
            sellable = cc_value >= self.config.min_ticket_cc
            if not sellable:
                logger.info("[%s] %s %s is dust (~%s CC < %s) — buying instead",
                            wallet.name, token_bal, pair.token_symbol, cc_value,
                            self.config.min_ticket_cc)
        return pair, sellable, token_bal, usdcx_bal, cc_bal
