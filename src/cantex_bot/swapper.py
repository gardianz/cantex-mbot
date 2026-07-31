"""SwapEngine: the single path every swap goes through.

quote -> guard -> (dry-run stop | live swap_and_confirm) -> record -> notify.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from cantex_sdk import CantexError, InstrumentId, SwapExecutedEvent, SwapQuote

from .guards import GuardResult, SwapGuard
from .store import Store, SwapRecord
from .telegram import TelegramNotifier
from .wallets import Wallet

logger = logging.getLogger(__name__)


def _is_max_fee_error(exc: object) -> bool:
    """True for the API's ``maxNetworkFee reached`` rejection.

    The network fee moves between the quote and the submit, so a quote that
    passed the guard can still be over the cap by the time the intent lands. The
    API then rejects it with HTTP 400 *before executing anything* — so nothing
    can have settled, and the condition is exactly what the fee guard blocks:
    a market state to wait out, not a failure.
    """
    # Tolerate maxNetworkFee / max_network_fee / "max network fee" spellings.
    text = str(exc).lower().replace("_", "").replace(" ", "")
    return "maxnetworkfee" in text


@dataclass
class SwapOutcome:
    wallet: str
    direction: str            # 'buy' | 'sell'
    sell_symbol: str
    buy_symbol: str
    sell_amount: Decimal
    executed: bool = False    # real swap submitted & confirmed
    submitted_attempt: bool = False  # live swap_and_confirm was called (may have settled even on error)
    counted: bool = False     # incremented the daily counter
    dry_run: bool = False
    buy_amount: Decimal = Decimal(0)
    quote: SwapQuote | None = None
    guard: GuardResult | None = None
    event: SwapExecutedEvent | None = None
    error: str | None = None
    reject_reasons: list[str] | None = None
    # The API refused the intent because the live network fee was over the cap.
    # A rejection, not an error — see _is_max_fee_error.
    fee_rejected: bool = False

    @property
    def ok(self) -> bool:
        return self.executed or (self.dry_run and self.error is None and not self.reject_reasons)


class SwapEngine:
    def __init__(
        self,
        guard: SwapGuard,
        store: Store,
        notifier: TelegramNotifier,
        *,
        dry_run: bool,
    ) -> None:
        self.guard = guard
        self.store = store
        self.notifier = notifier
        self.dry_run = dry_run

    async def execute_swap(
        self,
        wallet: Wallet,
        *,
        sell: InstrumentId,
        buy: InstrumentId,
        sell_amount: Decimal,
        sell_symbol: str,
        buy_symbol: str,
        direction: str,
        quiet_reject: bool = False,
        bypass_guards: bool = False,
        ignore_network_fee: bool = False,
    ) -> SwapOutcome:
        out = SwapOutcome(
            wallet=wallet.name,
            direction=direction,
            sell_symbol=sell_symbol,
            buy_symbol=buy_symbol,
            sell_amount=sell_amount,
            dry_run=self.dry_run,
        )
        tag = f"[{wallet.name}] {direction} {sell_amount} {sell_symbol}->{buy_symbol}"

        if sell_amount <= 0:
            out.error = "sell_amount <= 0"
            logger.warning("%s skipped: %s", tag, out.error)
            return out

        # 0. Ensure this wallet is authenticated (lazy).
        await wallet.ensure_auth()

        # 1. Quote
        try:
            quote = await wallet.sdk.get_swap_quote(sell_amount, sell, buy)
        except CantexError as exc:
            out.error = f"quote failed: {exc}"
            logger.error("%s %s", tag, out.error)
            await self.notifier.send(f"⚠️ {tag}\nQuote error: {exc}")
            return out
        out.quote = quote
        out.buy_amount = quote.returned_amount
        # Record the observed network fee + slippage + pool fee for today's stats.
        net, slip, pool = self.guard.quote_metrics(quote)
        self.store.record_fee(
            wallet.name, f"{sell_symbol}->{buy_symbol}", net, slip, pool
        )

        # 2. Guard — unless explicitly bypassed (manual 1x swap override).
        result = self.guard.evaluate(quote, ignore_network_fee=ignore_network_fee)
        out.guard = result
        if not result.ok:
            if bypass_guards:
                # Execute regardless: record the breached limits but do NOT
                # reject. Real-money risk — deliberately chosen in the CLI.
                logger.warning("%s GUARD BYPASSED: %s", tag, "; ".join(result.reasons))
            else:
                out.reject_reasons = result.reasons
                if quiet_reject:
                    # Polling mode: conditions not met yet is expected, not an error.
                    logger.info("%s waiting (fee/slippage too high): %s",
                                tag, "; ".join(result.reasons))
                else:
                    logger.warning("%s GUARD REJECT: %s", tag, "; ".join(result.reasons))
                    await self.notifier.send(
                        f"🛑 {tag}\nGuard reject: {'; '.join(result.reasons)}"
                    )
                return out

        # 3. Dry-run: record intent, count, stop.
        if self.dry_run:
            self._record_from_quote(wallet.name, direction, sell_symbol, buy_symbol,
                                    sell_amount, quote, dry=True)
            out.counted = True
            self.store.incr_daily(wallet.name)
            logger.info(
                "%s DRY-RUN ok (returns %s %s, fees admin=%s liq=%s net=%s)",
                tag, quote.returned_amount, buy_symbol,
                quote.fees.amount_admin, quote.fees.amount_liquidity,
                quote.fees.network_fee.amount,
            )
            await self.notifier.send(
                f"🧪 {tag}\nDRY-RUN → {quote.returned_amount} {buy_symbol}"
            )
            return out

        # 4. Live
        out.submitted_attempt = True  # from here on an error may still have settled
        # A bypass (or a waived fee limit) must also lift the SDK-level network-fee
        # cap, or the SDK would reject the swap the guard just allowed.
        max_fee = (Decimal("1000000000") if (bypass_guards or ignore_network_fee)
                   else self.guard.config.max_network_fee)
        try:
            event = await wallet.sdk.swap_and_confirm(
                sell_amount, sell, buy, max_network_fee=max_fee,
            )
        except CantexError as exc:
            if _is_max_fee_error(exc):
                # The fee rose between the quote and the submit. The API refused
                # the intent before executing it, so NOTHING settled: clear
                # submitted_attempt (no history reconciliation needed) and report
                # it as a rejection. Treating this as an error made a caller's
                # consecutive-failure counter climb on an ordinary market
                # condition until it gave up on the wallet entirely.
                out.submitted_attempt = False
                out.fee_rejected = True
                out.reject_reasons = [
                    f"network fee rose above {max_fee} between quote and submit"
                ]
                if quiet_reject:
                    logger.info("%s waiting (%s)", tag, out.reject_reasons[0])
                else:
                    logger.warning("%s FEE REJECT: %s", tag, out.reject_reasons[0])
                    await self.notifier.send(
                        f"🛑 {tag}\nFee reject: {out.reject_reasons[0]}"
                    )
                return out
            out.error = f"swap failed: {exc}"
            logger.error("%s %s", tag, out.error)
            await self.notifier.send(f"❌ {tag}\nSwap failed: {exc}")
            return out

        out.event = event
        out.executed = True
        out.buy_amount = event.output_amount
        self.store.record_swap(
            SwapRecord(
                wallet=wallet.name,
                direction=direction,
                sell_symbol=sell_symbol,
                buy_symbol=buy_symbol,
                sell_amount=event.input_amount,
                buy_amount=event.output_amount,
                admin_fee=event.admin_fee_amount,
                liquidity_fee=event.liquidity_fee_amount,
                network_fee=quote.fees.network_fee.amount,
                price=event.price,
                dry_run=False,
            )
        )
        out.counted = True
        self.store.incr_daily(wallet.name)
        logger.info(
            "%s EXECUTED: %s %s -> %s %s (price=%s)",
            tag, event.input_amount, sell_symbol, event.output_amount, buy_symbol,
            event.price,
        )
        await self.notifier.send(
            f"✅ {tag}\n{event.input_amount} {sell_symbol} → "
            f"{event.output_amount} {buy_symbol} @ {event.price}"
        )
        return out

    def _record_from_quote(
        self, wallet: str, direction: str, sell_symbol: str, buy_symbol: str,
        sell_amount: Decimal, quote: SwapQuote, *, dry: bool,
    ) -> None:
        self.store.record_swap(
            SwapRecord(
                wallet=wallet,
                direction=direction,
                sell_symbol=sell_symbol,
                buy_symbol=buy_symbol,
                sell_amount=sell_amount,
                buy_amount=quote.returned_amount,
                admin_fee=quote.fees.amount_admin,
                liquidity_fee=quote.fees.amount_liquidity,
                network_fee=quote.fees.network_fee.amount,
                price=quote.prices.trade,
                dry_run=dry,
            )
        )
