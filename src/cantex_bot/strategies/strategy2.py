"""Strategy 2: like Strategy 1, but auto-selects the lowest-fee destination.

Same cycle as Strategy 1 (base <-> token round trips, daily target, guards,
dashboard), except the destination token is not round-robin: on every buy the
bot picks the pool with the **lowest network fee** among the chosen candidates.
The base token never changes — only the destination.

Once a wallet has bought a token it is "stuck" in it: the bot will not switch to
another token. It keeps trying to sell THAT token back to the base, waiting for
its fee to fall under the guard, and only after it is back at the base does it
pick a fresh lowest-fee destination.
"""
from __future__ import annotations

import logging

from cantex_sdk import CantexError

from .strategy1 import Strategy1

logger = logging.getLogger(__name__)


class Strategy2(Strategy1):
    name = "strategy2"
    label = "Strategy2"

    async def _pick(self, wallet, pairs, usdcx, cc, state):
        """Sell a held (stuck) token back to base first; otherwise buy the
        lowest network-fee destination. Returns
        ``(pair, sellable, token_bal, usdcx_bal, cc_bal)``."""
        info = await wallet.sdk.get_account_info()
        usdcx_bal = info.get_balance(usdcx)
        cc_bal = info.get_balance(cc)

        # 1. Stuck in a destination token? sell THAT one back to base — never
        #    switch tokens while holding (guards make it wait for the fee to fall).
        for pair in pairs:
            bal = info.get_balance(pair.token)
            if bal > 0:
                cc_value = await self._token_cc_value(wallet, pair.token, bal, cc)
                if cc_value >= self.config.min_ticket_cc:
                    return pair, True, bal, usdcx_bal, cc_bal

        # 2. Holding base: pick the lowest-network-fee destination to buy.
        best = await self._lowest_fee_pair(wallet, pairs, usdcx, state["notional"])
        return best, False, info.get_balance(best.token), usdcx_bal, cc_bal

    async def _lowest_fee_pair(self, wallet, pairs, usdcx, notional):
        """Quote base->token for every candidate and return the pool with the
        smallest network fee. Each observed fee is recorded so the dashboard's
        PAIR FEES panel stays current for all pairs. Falls back to the first
        pair if none could be quoted."""
        best = None
        best_fee = None
        for pair in pairs:
            try:
                q = await wallet.sdk.get_swap_quote(notional, usdcx, pair.token)
            except CantexError as exc:
                logger.debug("[%s] fee quote %s failed: %s",
                             wallet.name, pair.token_symbol, exc)
                continue
            fee, slip, pool = self.engine.guard.quote_metrics(q)
            self.store.record_fee(
                wallet.name, f"{self.base_symbol}->{pair.token_symbol}", fee, slip, pool)
            if best_fee is None or fee < best_fee:
                best_fee, best = fee, pair
        if best is not None:
            logger.info("[%s] lowest-fee target: %s (network fee %s)",
                        wallet.name, best.token_symbol, best_fee)
        return best or pairs[0]
