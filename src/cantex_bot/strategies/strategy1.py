"""Strategy 1: cycle USDCX <-> every USDCX pair, back and forth.

Per wallet, per day:
  * Buy side  (USDCX -> token): sized to the market value of N CC tokens.
  * Sell side (token -> USDCX): sells 100% of the token's unlocked balance.
Alternates buy/sell across all USDCX pairs (round-robin) until the wallet hits
its daily swap target. Every swap goes through SwapEngine (guards enforced).
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import random
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
from ..swapper import SwapEngine, is_rate_limited, is_transient
from ..telegram import TelegramNotifier
from ..store import Store
from ..wallets import Wallet, WalletManager
from ..webclient import WebClient
from .base import Strategy

logger = logging.getLogger(__name__)


class Strategy1(Strategy):
    name = "strategy1"
    label = "Strategy1"          # display name in logs / Telegram (subclasses override)
    # Tries for the one quote a wallet cannot start without (see
    # `_initial_notional`), and the seconds between them (grows per attempt).
    # Every later quote is retried by the loop itself.
    PRICE_RETRIES = 5
    PRICE_RETRY_BACKOFF = 2.0
    # Request budget. A wallet waiting on a guard used to spend FOUR requests
    # per poll — account info, a dust-check quote, a cycle-loss quote and the
    # swap quote — when only the last one carries news: nothing it holds
    # changes until it swaps. Balances are reused for BALANCE_TTL seconds and
    # dropped the moment a swap is submitted; a holding's CC value (only ever
    # compared with the 10 CC min ticket) for VALUE_TTL seconds per amount.
    BALANCE_TTL = 30.0
    VALUE_TTL = 300.0
    # Ceiling for the back-off after repeated 429s / network failures.
    TRANSIENT_MAX_BACKOFF = 60.0
    # A wallet parked for "saldo kurang" re-reads its balance this often and
    # resumes the same day once it can afford a buy again — funds arrive
    # mid-day (an accepted incoming transfer, a distribute, a deposit), and
    # idling to midnight with the money already there wastes the day.
    FUNDS_POLL_SECONDS = 60.0

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
        # Per-wallet cache of (monotonic_ts, base value of cc_units CC).
        self._notional_cache: dict[str, tuple[float, Decimal]] = {}
        # When a (wallet, route) first hit a guard rejection, and whether the
        # stuck warning has already been raised for it.
        self._guard_since: dict[tuple[str, str], float] = {}
        self._guard_warned: set[tuple[str, str]] = set()
        # Watchdog bookkeeping: when each wallet last completed a loop pass, the
        # task running it, and the wallets deliberately parked until tomorrow.
        self._last_progress: dict[str, float] = {}
        self._wallet_tasks: dict[str, asyncio.Task] = {}
        self._idle_until_day: set[str] = set()
        self._stall_reported: set[str] = set()
        # Request-budget caches (see BALANCE_TTL / VALUE_TTL) and the per-wallet
        # count of back-to-back transient failures that sizes the back-off.
        self._info_cache: dict[str, tuple[float, object]] = {}
        self._value_cache: dict[tuple[str, InstrumentId, Decimal],
                                tuple[float, Decimal]] = {}
        self._transient_streak: dict[str, int] = {}

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
        watchdog = asyncio.create_task(self._watchdog(stop))
        try:
            results = await asyncio.gather(
                *(self._run_wallet(w, stop) for w in self.manager.wallets.values()),
                return_exceptions=True,
            )
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
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
        task = asyncio.current_task()
        if task is not None:
            self._wallet_tasks[wallet.name] = task
        self._mark_progress(wallet.name)
        with wallet_logs(wallet.name):
            try:
                while True:
                    try:
                        await self._trade_wallet(wallet, stop)
                        return
                    except Exception as exc:  # noqa: BLE001 - classified below
                        # A throttled or dropped request that escaped the loop's
                        # own handling — authenticating or loading the market at
                        # start-up, say — says nothing about the wallet. Ending
                        # it would leave it dead until the whole run restarts,
                        # so back off and start its loop again. Restarting is
                        # safe: the day's count comes back from the local
                        # counter and the history, and a held token is sold.
                        if not is_transient(exc) or stop.is_set():
                            raise
                        logger.warning("[%s] %s — restarting wallet loop",
                                       wallet.name, exc)
                        await self._transient_pause(
                            wallet, "", is_rate_limited(exc), exc, stop)
                        if stop.is_set():
                            return
                        self._mark_progress(wallet.name)
            except Exception as exc:  # noqa: BLE001 - per-wallet isolation
                # Report it here, not after the gather in `run()`. That gather
                # only returns once EVERY wallet has, and a wallet polling a
                # guard may poll until the run is stopped — so a crash reported
                # there stays invisible for the rest of the day while the
                # dashboard still shows the wallet's last status.
                logger.exception("[%s] %s wallet crashed: %s",
                                 wallet.name, self.label, exc)
                self._st(wallet.name, status=run_status.ERROR, route="",
                         plan=f"crash: {exc}"[:60])
                if self.run_state is not None:
                    self.run_state.finish(wallet.name, status=run_status.ERROR)
                await self.notifier.send(
                    f"❌ {self.label} [{wallet.name}] crashed: {exc}")
            finally:
                self._last_progress.pop(wallet.name, None)
                self._wallet_tasks.pop(wallet.name, None)

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

        buy_notional = await self._initial_notional(wallet, cc, usdcx, stop)
        if buy_notional <= 0:
            return
        self._notional_cache[wallet.name] = (time.monotonic(), buy_notional)
        logger.info(
            "[%s] buy notional = %s %s (= %s CC), %d pairs",
            wallet.name, buy_notional, self.base_symbol,
            self.config.cc_units, len(pairs),
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
            self._mark_progress(wallet.name)
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

            # Re-price the buy: cc_units is a CC amount but the swap is
            # denominated in the base, so a move in CC/base changes what every
            # buy is worth. Priced once before the loop it goes stale — and since
            # a wallet idles through the UTC rollover and keeps going, "once" can
            # mean days (a 17% CC move turned a 110 CC buy into 94 CC).
            buy_notional = await self._buy_notional(wallet, cc, usdcx, buy_notional)
            state["notional"] = buy_notional

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
                self._park_until_day(wallet.name)
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
                    self._park_until_day(wallet.name)
                    if not await self._wait_next_day(stop):
                        break
                    continue
            if insufficient_streak >= self.config.insufficient_retries:
                logger.warning("[%s] insufficient balance after %d retries — "
                               "waiting for funds or the next UTC day",
                               wallet.name, insufficient_streak)
                self._st(wallet.name, status=run_status.STOPPED,
                         route="", plan="saldo kurang")
                self._park_until_day(wallet.name)
                woke = await self._wait_for_funds(
                    wallet, stop, pairs, usdcx, cc, state["notional"])
                if woke is None:
                    break
                if woke == "funds":
                    insufficient_streak = 0
                    self._st(wallet.name, status=run_status.RUNNING,
                             route="", plan="saldo masuk")
                    await self.notifier.send(
                        f"💰 {self.label} [{wallet.name}] funds arrived — "
                        f"trading again")
                continue

            try:
                pair, sellable, token_bal, usdcx_bal, cc_bal = await self._pick(
                    wallet, pairs, usdcx, cc, state)
            except CantexError as exc:
                # A 429 on the balance read used to end the wallet outright.
                if not is_transient(exc):
                    raise
                await self._transient_pause(
                    wallet, "", is_rate_limited(exc), exc, stop)
                continue
            # Taken out of `state` now, so a pass that ends early (saldo kurang,
            # brake hold) cannot leave it behind for a later pass to use stale.
            buy_hint = state.pop("buy_quote", None)
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
                logger.info("[%s] insufficient %s (%s < %s), skip %s",
                            wallet.name, self.base_symbol, usdcx_bal,
                            buy_notional, tok)
                # Each strike must be a FRESH read: a balance cached from just
                # before a sell settled would otherwise be counted four times
                # in a row and park a wallet that has the money.
                self._forget_balances(wallet.name)
                await asyncio.sleep(self._insufficient_pause())
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
            # The brake and the sell quote the very same swap (all of the token
            # back to the base), so quote it once and hand it to both.
            sell_quote = None
            if step == "sell" and (cap > 0 or min_profit > 0):
                loss_pct, sell_quote = await self._cycle_loss_and_quote(
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
                    quote=sell_quote,
                )
            else:
                out = await self.engine.execute_swap(
                    wallet, sell=usdcx, buy=pair.token, sell_amount=buy_notional,
                    sell_symbol=usym, buy_symbol=tok,
                    direction="buy", quiet_reject=True,
                    quote=self._buy_quote_from(buy_hint, tok, buy_notional),
                )
            # Anything that reached the exchange may have moved a balance.
            if (getattr(out, "submitted_attempt", False)
                    or getattr(out, "executed", False) or out.counted):
                self._forget_balances(wallet.name)
            if getattr(out, "transient", False):
                # The quote was throttled or dropped — nothing was submitted.
                # Counting it as a failed swap is how a burst of 429s used to
                # end healthy wallets with "stopped: repeated errors".
                await self._transient_pause(
                    wallet, route, getattr(out, "rate_limited", False), out.error,
                    stop)
                continue
            self._transient_streak.pop(wallet.name, None)

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
                self._forget_balances(wallet.name)
                await asyncio.sleep(self._insufficient_pause())
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
                    self._clear_guard_wait(wallet.name, route)
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
                self._clear_guard_wait(wallet.name, route)
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
                    # Name the limit that is actually biting, and escalate to
                    # "stuck" once the wait stops being plausible — a wallet that
                    # can never pass a guard used to poll for ever while the
                    # others finished, showing only a row that never moved.
                    label, static = self._blocking_guard(out)
                    plan = await self._note_guard_wait(
                        wallet, route, label, static)
                    delay = self._poll_interval(out)
                if force_sl:
                    # Stop armed but a guard says no — say so, so the row
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

    async def _web_swaps_today(self, wallet: Wallet, ttl: float = 60.0) -> int:
        """Successful swaps today (UTC) from web history, 0 if no web.

        Cached per wallet for ``ttl`` seconds so the fee-polling loop does not
        hammer the history endpoint; the cache is invalidated after any swap.
        60s, not less: `_current_done` also counts this run's own swaps and the
        local counter, so between swaps the history only catches up on
        indexing lag — and the portfolio sweep mirrors it every refresh anyway.
        At 15s it was 2 requests a second across 30 waiting wallets.
        """
        if wallet.web is None:
            return 0
        now = time.monotonic()
        cached = self._web_cache.get(wallet.name)
        if cached and now - cached[0] < ttl:
            return cached[1]
        try:
            # The endpoint returns only the 50 newest rows, so at a target of 50
            # or more it can no longer see the whole day. Mirror each poll and
            # count from the mirror, which keeps the rows that fell off.
            trades = await wallet.web.fetch_trading_history()
            self.store.record_trades(wallet.name, trades)
            today = datetime.now(timezone.utc).date()
            val = self.store.count_trades(wallet.name, today, today)
        except Exception as exc:  # noqa: BLE001 - one bad poll, not a dead wallet
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
            self.store.record_trades(wallet.name, trades)
            today = datetime.now(timezone.utc).date()
            val = WebClient.loss_between(
                self.store.trades_between(wallet.name, today, today),
                usdcx_symbol=self.base_symbol, start=today, end=today)
        except Exception as exc:  # noqa: BLE001 - one bad poll, not a dead wallet
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
        pct, _quote = await self._cycle_loss_and_quote(
            wallet, token, token_symbol, amount, base)
        return pct

    async def _cycle_loss_and_quote(
        self, wallet: Wallet, token: InstrumentId, token_symbol: str,
        amount: Decimal, base: InstrumentId,
    ):
        """``(loss_pct, quote)`` — the brake's reading plus the sell quote it
        was taken from, which is exactly the quote the sell itself needs. No
        quote is made when there is no recorded buy to measure against: the
        sell then quotes for itself, as before."""
        spent = self.store.last_buy_cost(wallet.name, self.base_symbol, token_symbol)
        if spent is None or spent <= 0:
            return None, None
        quote = await self._quote_or_none(wallet, amount, token, base)
        if quote is None:
            return None, None
        return (spent - quote.returned_amount) / spent * Decimal(100), quote

    @staticmethod
    async def _quote_or_none(wallet: Wallet, amount: Decimal,
                             sell: InstrumentId, buy: InstrumentId):
        """A pricing quote, or None if it failed (execute_swap then re-quotes
        and classifies the failure itself)."""
        try:
            return await wallet.sdk.get_swap_quote(amount, sell, buy)
        except CantexError:
            return None

    @staticmethod
    def _buy_quote_from(hint, token_symbol: str, notional: Decimal):
        """The buy quote a `_pick` override already paid for (left in
        ``state["buy_quote"]`` this same pass), if it is for this exact buy."""
        if hint is None:
            return None
        sym, amount, quote = hint
        return quote if sym == token_symbol and amount == notional else None

    async def _transient_pause(
        self, wallet: Wallet, route: str, rate_limited: bool, why: object,
        stop: asyncio.Event,
    ) -> None:
        """Back off after a throttled or dropped request, longer each time.

        The SDK has already retried four times (1+2+4s), so hitting it again at
        the normal poll pace just keeps the limit tripped for every wallet on
        the same egress. Doubles per consecutive failure, capped, and resets on
        the first request that gets through.
        """
        n = self._transient_streak.get(wallet.name, 0) + 1
        self._transient_streak[wallet.name] = n
        delay = min(self.TRANSIENT_MAX_BACKOFF,
                    max(self.config.poll_max_seconds, 1.0) * 2 ** (n - 1))
        # Jitter: wallets throttled by the same burst would otherwise all come
        # back in the same second and trip the limit together again.
        delay *= random.uniform(0.5, 1.0)
        label = "rate limit" if rate_limited else "gangguan jaringan"
        self._st(wallet.name, status=run_status.WAITING, route=route,
                 plan=f"{label}, jeda {delay:.0f}s")
        logger.info("[%s] %s (#%d) — backing off %.0fs: %s",
                    wallet.name, label, n, delay, why)
        # Up to a minute: wake on stop rather than hold the run open that long.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)

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

    def _mark_progress(self, name: str) -> None:
        """One loop pass completed. Anything slower than the watchdog window
        between two of these means the coroutine is parked on an await."""
        self._last_progress[name] = time.monotonic()
        self._idle_until_day.discard(name)
        self._stall_reported.discard(name)

    def _park_until_day(self, name: str) -> None:
        """Idling to the next UTC day is deliberate, not a stall — the watchdog
        must not report a wallet that has simply finished for today."""
        self._idle_until_day.add(name)

    async def _watchdog(self, stop: asyncio.Event) -> None:
        """Report any wallet whose loop has stopped advancing, with its stack.

        Every network call in the loop carries a timeout, so a freeze that
        outlasts all of them cannot be diagnosed from the symptoms — the same
        row reads "proses swap" whether the swap is slow or the coroutine is
        parked for ever. ``Task.print_stack`` names the exact await instead.
        """
        limit = self.config.stall_timeout_seconds
        if limit <= 0:
            return
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(60.0, limit))
                return
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            for name, last in list(self._last_progress.items()):
                if name in self._idle_until_day or name in self._stall_reported:
                    continue
                idle = now - last
                if idle < limit:
                    continue
                self._stall_reported.add(name)
                task = self._wallet_tasks.get(name)
                where = "task not found"
                if task is not None and not task.done():
                    buf = io.StringIO()
                    try:
                        task.print_stack(limit=14, file=buf)
                        where = buf.getvalue()
                    except Exception as exc:  # noqa: BLE001 - diagnostics only
                        where = f"stack unavailable: {exc}"
                logger.error(
                    "[%s] STALLED %.0fs with no loop progress — parked here:\n%s",
                    name, idle, where)
                await self.notifier.send(
                    f"🧊 {self.label} [{name}] stalled {idle / 60:.0f}m with no "
                    f"progress — stack written to cantex_bot.log"
                )

    def _blocking_guard(self, outcome) -> tuple[str, bool]:
        """``(label, static)`` for whatever the guard actually rejected.

        The status column used to read "wait fee" for every rejection, so a leg
        blocked on slippage or a pool fee looked like it was waiting out a
        network fee that would come down. Read the measured values rather than
        the reason text, and say which limit is biting.

        ``static`` marks a pool fee: that is a property of the pool, so waiting
        for it to fall is waiting for nothing.
        """
        details = outcome.guard.details if outcome.guard else {}
        limits = self.engine.guard.config
        pool = details.get("pool_fee_pct")
        slip = details.get("slippage_pct")
        fee = details.get("network_fee")
        if pool is not None and pool > limits.max_pool_fee_pct:
            return f"pool {pool:.3f}>{limits.max_pool_fee_pct}", True
        if slip is not None and slip > limits.max_slippage:
            return f"slip {slip:.3f}>{limits.max_slippage}", False
        if fee is not None and fee > limits.max_network_fee:
            return f"fee {fee:.3f}", False
        return "guard", False

    async def _note_guard_wait(
        self, wallet: Wallet, route: str, label: str, static: bool,
    ) -> str:
        """Track how long this leg has been rejected; return the status text.

        A wallet that can never satisfy a guard would otherwise poll for ever
        while the others reach their target — visible only as a row that never
        moves. Past ``guard_wait_seconds`` it says so, and says why, once.
        """
        limit = self.config.guard_wait_seconds
        if limit <= 0:
            return f"tunggu {label}"
        key = (wallet.name, route)
        first = self._guard_since.setdefault(key, time.monotonic())
        waited = time.monotonic() - first
        if waited < limit:
            return f"tunggu {label}"
        if key not in self._guard_warned:
            self._guard_warned.add(key)
            logger.warning(
                "[%s] stuck %.0fm on %s: %s%s", wallet.name, waited / 60, route,
                label,
                " — a pool fee does not change, so this will not clear itself"
                if static else "")
            await self.notifier.send(
                f"⚠️ {self.label} [{wallet.name}] stuck {waited / 60:.0f}m on "
                f"{route} — {label}"
                + (" (pool fee is fixed; raise max_pool_fee_pct or drop this pair)"
                   if static else "")
            )
        return f"stuck: {label}"

    def _clear_guard_wait(self, wallet_name: str, route: str) -> None:
        key = (wallet_name, route)
        self._guard_since.pop(key, None)
        self._guard_warned.discard(key)

    async def _buy_notional(
        self, wallet: Wallet, cc: InstrumentId, usdcx: InstrumentId,
        current: Decimal,
    ) -> Decimal:
        """Base-currency value of ``cc_units`` CC, re-quoted every
        ``notional_ttl_seconds``.

        Falls back to ``current`` when the quote fails, so a transient error can
        never shrink the buy size (or drop it to zero and stall the wallet).
        """
        now = time.monotonic()
        hit = self._notional_cache.get(wallet.name)
        if hit is not None and now - hit[0] < self.config.notional_ttl_seconds:
            return hit[1]
        priced = await self._price_cc_in_usdcx(wallet, cc, usdcx)
        if priced <= 0:
            return current
        if current > 0:
            drift = abs(priced - current) / current
            if drift >= Decimal("0.01"):      # only worth a line when it matters
                logger.info("[%s] buy size re-priced %s -> %s %s (= %s CC, %+.1f%%)",
                            wallet.name, current, priced, self.base_symbol,
                            self.config.cc_units,
                            float((priced - current) / current * 100))
        self._notional_cache[wallet.name] = (now, priced)
        return priced

    async def _initial_notional(
        self, wallet: Wallet, cc: InstrumentId, usdcx: InstrumentId,
        stop: asyncio.Event,
    ) -> Decimal:
        """Price the first buy, retrying a failed quote. 0 = give up (reported).

        A single transient quote error used to end the wallet right here: the
        loop was never entered, so no route, plan or terminal status was ever
        written, and the row read "running" with 0 swaps for the rest of the day
        while every other wallet traded. The buy size is re-quoted every
        `notional_ttl_seconds` once the loop runs, so this one quote is the only
        place where a failure is fatal — retry it, and if it really cannot be
        priced, stop the wallet loudly instead of silently.
        """
        for attempt in range(1, self.PRICE_RETRIES + 1):
            if stop.is_set():
                return Decimal(0)
            priced = await self._price_cc_in_usdcx(wallet, cc, usdcx)
            if priced > 0:
                return priced
            if attempt == self.PRICE_RETRIES:
                break
            self._st(wallet.name, status=run_status.WAITING, route="",
                     plan=f"harga CC gagal {attempt}/{self.PRICE_RETRIES}")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    stop.wait(), timeout=self.PRICE_RETRY_BACKOFF * attempt)
        logger.error("[%s] could not price %s CC in %s after %d tries — "
                     "wallet stopped", wallet.name, self.config.cc_units,
                     self.base_symbol, self.PRICE_RETRIES)
        self._st(wallet.name, status=run_status.STOPPED, route="",
                 plan="harga CC gagal")
        if self.run_state is not None:
            self.run_state.finish(wallet.name, status=run_status.STOPPED)
        await self.notifier.send(
            f"⚠️ {self.label} [{wallet.name}] could not price "
            f"{self.config.cc_units} CC in {self.base_symbol} — wallet stopped"
        )
        return Decimal(0)

    async def _price_cc_in_usdcx(
        self, wallet: Wallet, cc: InstrumentId, usdcx: InstrumentId,
    ) -> Decimal:
        """Base-currency value of ``cc_units`` CC, via a pricing quote (no swap).

        The parameter is still named ``usdcx`` for history; it is whatever base
        the run was started with, which is often not USDCX.
        """
        try:
            quote = await wallet.sdk.get_swap_quote(self.config.cc_units, cc, usdcx)
            return quote.returned_amount
        except CantexError as exc:
            logger.error("[%s] CC pricing quote failed: %s", wallet.name, exc)
            return Decimal(0)

    def _insufficient_pause(self) -> float:
        """Gap between "saldo kurang" strikes. Four strikes at the 1s cooldown
        span ~4s, shorter than the exchange can take to show a swap that just
        settled; spacing them at the slow poll gives it ~15s."""
        return max(self.config.cooldown_seconds, self.config.poll_max_seconds)

    async def _wait_for_funds(
        self, wallet: Wallet, stop: asyncio.Event, pairs, base: InstrumentId,
        cc: InstrumentId, need: Decimal,
    ) -> str | None:
        """Idle a wallet that cannot trade until it can, or until the next UTC
        day. ``"funds"`` = resume now, ``"day"`` = the day rolled over (the
        loop resets as usual), ``None`` = stopped.

        Re-reads the balance every FUNDS_POLL_SECONDS — one request a minute,
        against a whole day of a funded wallet sitting idle. It can trade
        again when CC covers the network fee the guard allows AND either the
        base covers a buy or it holds a pair token worth selling. The second
        case is the one that bit: mulyanayanu was restarted holding TRKXAI,
        read 7.97 USDC.B against a 12.04 buy, and parked for the day with the
        position still open — waiting for base that only the sell produces.
        """
        need_cc = self.engine.guard.config.max_network_fee
        day = datetime.now(timezone.utc).date()
        while True:
            # Checked explicitly: wait_for with a zero timeout (right at
            # midnight) times out without looking at an already-set event.
            if stop.is_set():
                return None
            now = datetime.now(timezone.utc)
            midnight = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            wait = min(self.FUNDS_POLL_SECONDS,
                       max((midnight - now).total_seconds(), 0.0))
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
                return None
            except asyncio.TimeoutError:
                pass
            if datetime.now(timezone.utc).date() != day:
                return "day"
            self._forget_balances(wallet.name)
            try:
                info = await self._account_info(wallet)
            except Exception as exc:  # noqa: BLE001 - keep waiting, try again
                logger.debug("[%s] funds check failed: %s", wallet.name, exc)
                continue
            have, have_cc = info.get_balance(base), info.get_balance(cc)
            if have_cc < need_cc:
                continue
            if have >= need:
                logger.info("[%s] funds arrived: %s %s (need %s), %s CC — "
                            "resuming", wallet.name, have, self.base_symbol,
                            need, have_cc)
                return "funds"
            for pair in pairs:
                held = info.get_balance(pair.token)
                if held <= 0:
                    continue
                value = await self._token_cc_value(wallet, pair.token, held, cc)
                if self._is_sellable(wallet, pair.token_symbol, held, value):
                    logger.info("[%s] holding %s %s to sell — resuming",
                                wallet.name, held, pair.token_symbol)
                    return "funds"

    async def _account_info(self, wallet: Wallet):
        """Account info, reused for BALANCE_TTL seconds.

        Only a swap moves this wallet's balances (and that drops the cache), so
        re-reading them on every guard poll bought nothing. The TTL is there
        for the one thing the bot does not do itself: a deposit arriving.
        """
        now = time.monotonic()
        hit = self._info_cache.get(wallet.name)
        if hit is not None and now - hit[0] < self.BALANCE_TTL:
            return hit[1]
        info = await wallet.sdk.get_account_info()
        self._info_cache[wallet.name] = (now, info)
        return info

    def _forget_balances(self, wallet_name: str) -> None:
        self._info_cache.pop(wallet_name, None)

    async def _token_balance(self, wallet: Wallet, token: InstrumentId) -> Decimal:
        info = await self._account_info(wallet)
        return info.get_balance(token)

    async def _balances(
        self, wallet: Wallet, token: InstrumentId, usdcx: InstrumentId,
        cc: InstrumentId,
    ) -> tuple[Decimal, Decimal, Decimal]:
        """(token, usdcx, cc) balances from a single account-info call."""
        info = await self._account_info(wallet)
        return (info.get_balance(token), info.get_balance(usdcx),
                info.get_balance(cc))

    async def _token_cc_value(
        self, wallet: Wallet, token: InstrumentId, amount: Decimal, cc: InstrumentId,
    ) -> Decimal | None:
        """CC value of `amount` of `token`, via a pricing quote.

        **None when the quote fails — never 0.** It used to return 0, which
        reads as "dust": under a 429 a wallet holding ~110 CC of eXAU decided
        it held nothing, tried to BUY with the base it had already spent, hit
        "saldo kurang" four times and parked until the next UTC day, still
        holding the token.

        Cached per exact amount for VALUE_TTL: the result is only compared
        with the 10 CC min ticket, and a holding of ~110 CC is not going to
        cross that between two polls.
        """
        key = (wallet.name, token, amount)
        now = time.monotonic()
        hit = self._value_cache.get(key)
        if hit is not None and now - hit[0] < self.VALUE_TTL:
            return hit[1]
        q = await self._quote_or_none(wallet, amount, token, cc)
        if q is None:
            return None
        if len(self._value_cache) > 512:          # amounts change per trade
            self._value_cache.clear()
        self._value_cache[key] = (now, q.returned_amount)
        return q.returned_amount

    def _is_sellable(self, wallet: Wallet, token_symbol: str, amount: Decimal,
                     cc_value: Decimal | None) -> bool:
        """Whether a holding is worth selling. An unknown value (quote failed)
        counts as sellable: flipping to a buy on a maybe is the same mistake
        as firing the opposite leg on an unconfirmed swap."""
        if cc_value is None:
            logger.info("[%s] %s %s: value unknown (quote failed) — treating "
                        "as sellable", wallet.name, amount, token_symbol)
            return True
        if cc_value < self.config.min_ticket_cc:
            logger.info("[%s] %s %s is dust (~%s CC < %s) — buying instead",
                        wallet.name, amount, token_symbol, cc_value,
                        self.config.min_ticket_cc)
            return False
        return True

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
            sellable = self._is_sellable(wallet, pair.token_symbol, token_bal,
                                         cc_value)
        return pair, sellable, token_bal, usdcx_bal, cc_bal
