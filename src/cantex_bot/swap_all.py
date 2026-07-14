"""One-shot swaps across a chosen set of wallets, tokens, and amount.

The interactive CLI collects: which wallets, a direction (buy USDCX->token /
sell token->USDCX / swap token->token), which tokens, and an amount that is
either an absolute value or a percent of the relevant balance.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .markets import MarketMap
from . import runstate as run_status
from .swapper import SwapEngine, SwapOutcome
from .wallets import WalletManager

logger = logging.getLogger(__name__)


class AmountError(Exception):
    pass


@dataclass(frozen=True)
class AmountSpec:
    """A swap size: either an absolute amount or a percent of balance."""

    value: Decimal
    is_percent: bool
    raw: str

    @classmethod
    def parse(cls, text: str) -> "AmountSpec":
        s = (text or "").strip()
        if not s:
            raise AmountError("empty amount")
        is_percent = s.endswith("%")
        num = s[:-1].strip() if is_percent else s
        try:
            value = Decimal(num)
        except InvalidOperation:
            raise AmountError(f"not a number: {text!r}") from None
        if value <= 0:
            raise AmountError("amount must be > 0")
        if is_percent and value > 100:
            raise AmountError("percent must be <= 100")
        return cls(value=value, is_percent=is_percent, raw=s)

    def resolve(self, balance: Decimal) -> Decimal:
        """Actual amount to sell given the current balance of the sell token."""
        if self.is_percent:
            return (balance * self.value) / Decimal(100)
        return self.value

    def __str__(self) -> str:
        return f"{self.value}%" if self.is_percent else str(self.value)


async def swap_selected(
    manager: WalletManager,
    engine: SwapEngine,
    *,
    wallet_names: list[str],
    token_symbols: list[str],
    usdcx_symbol: str,
    direction: str,
    amount: AmountSpec,
    sell_symbol: str | None = None,
    bypass_guards: bool = False,
    run_state=None,
    cooldown: float = 1.0,
) -> dict[str, list[SwapOutcome]]:
    """One swap per (wallet, token). ``direction`` is 'buy', 'sell', or 'swap'.

    buy  = USDCX -> token, sized from the USDCX balance.
    sell = token -> USDCX, sized from the token balance.
    swap = ``sell_symbol`` -> token (token->token), sized from the sell token's
           balance; the Cantex swap endpoint routes multi-hop (e.g. via CC), so
           any pool token is reachable from any other. A buy token equal to the
           sell token is skipped.
    Balances are re-read before each swap so a percent never over-spends.
    ``bypass_guards`` executes each swap regardless of the fee/slippage guards
    (a manual override — real-money risk).
    """
    if direction == "swap" and not sell_symbol:
        raise ValueError("direction 'swap' requires sell_symbol")

    def _st(name: str, **kw) -> None:
        """Publish per-wallet progress to the dashboard (ROUTE/STATUS columns),
        if a run_state was provided."""
        if run_state is not None:
            run_state.set(name, **kw)

    # Mark the involved wallets active so the dashboard lights up their rows.
    # SWAP d/t shows progress: done = successful swaps, target = planned swaps.
    if run_state is not None:
        for name in wallet_names:
            v = run_state.view(name)
            v.active = True
            v.finished = False
            v.status = run_status.RUNNING
            v.route = ""
            v.plan = "queued"
            v.done = 0
            v.target = len(token_symbols)

    results: dict[str, list[SwapOutcome]] = {}
    for name in wallet_names:
        wallet = manager.get(name)
        await wallet.ensure_auth()
        market = await MarketMap.build(wallet.sdk)
        usdcx = market.instrument(usdcx_symbol)
        sell_inst = market.instrument(sell_symbol) if direction == "swap" else None
        outcomes: list[SwapOutcome] = []
        for sym in token_symbols:
            try:
                token = market.instrument(sym)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] unknown token %s: %s", name, sym, exc)
                continue
            info = await wallet.sdk.get_account_info()
            if direction == "buy":
                sell_amount = amount.resolve(info.get_balance(usdcx))
                sell, buy, ssym, bsym = usdcx, token, usdcx_symbol, sym
            elif direction == "sell":
                sell_amount = amount.resolve(info.get_balance(token))
                sell, buy, ssym, bsym = token, usdcx, sym, usdcx_symbol
            else:  # "swap" token -> token
                if sym.upper() == sell_symbol.upper():
                    continue  # cannot swap a token into itself
                sell_amount = amount.resolve(info.get_balance(sell_inst))
                sell, buy, ssym, bsym = sell_inst, token, sell_symbol, sym
            if sell_amount <= 0:
                logger.info("[%s] skip %s: zero amount", name, sym)
                continue
            route = f"{direction} {ssym}→{bsym}"
            _st(name, status=run_status.SWAPPING, route=route, plan="proses swap")
            out = await engine.execute_swap(
                wallet, sell=sell, buy=buy, sell_amount=sell_amount,
                sell_symbol=ssym, buy_symbol=bsym, direction=direction,
                bypass_guards=bypass_guards,
            )
            outcomes.append(out)
            done = sum(1 for o in outcomes if o.ok)
            if out.ok:
                _st(name, status=run_status.RUNNING, route=route,
                    plan="swap berhasil", done=done)
            elif out.reject_reasons:
                _st(name, status=run_status.WAITING, route=route,
                    plan="guard reject", done=done)
            else:
                _st(name, status=run_status.ERROR, route=route,
                    plan="swap gagal", done=done)
            await asyncio.sleep(cooldown)
        # Freeze the wallet's final line on the dashboard (keeps STATUS, clears
        # the live ROUTE) instead of reverting to the rebate note.
        if run_state is not None:
            ok = sum(1 for o in outcomes if o.ok)
            status = run_status.DONE if ok and ok == len(outcomes) else run_status.STOPPED
            run_state.finish(name, status=status)
        results[name] = outcomes
    return results
