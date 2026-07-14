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
            out = await engine.execute_swap(
                wallet, sell=sell, buy=buy, sell_amount=sell_amount,
                sell_symbol=ssym, buy_symbol=bsym, direction=direction,
                bypass_guards=bypass_guards,
            )
            outcomes.append(out)
            await asyncio.sleep(cooldown)
        results[name] = outcomes
    return results
