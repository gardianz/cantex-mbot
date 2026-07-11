"""Pre-swap guards: reject a swap whose quote breaches fee/slippage limits.

Thresholds are in **percent** as a human reads them: ``max_pool_fee_pct = 0.1``
means 0.10%. The Cantex API returns slippage and pool fee as fractions
(0.001 = 0.10%), so we convert them to percent (x100) before comparing.
``max_network_fee`` is absolute, in CC. The ``details`` are also in percent, so a
dry-run's log lines line up 1:1 with the config units.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from cantex_sdk import SwapQuote

from .config import GuardConfig

_PCT = Decimal(100)  # API returns fractions; config/limits are in percent


class GuardRejected(Exception):
    """Raised when a swap quote violates one or more guards."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Decimal] = field(default_factory=dict)


class SwapGuard:
    def __init__(self, config: GuardConfig) -> None:
        self.config = config

    @staticmethod
    def _max_pool_fee(quote: SwapQuote) -> Decimal:
        if not quote.pools:
            return Decimal(0)
        return max(p.fees.fee_percentage for p in quote.pools)

    def evaluate(self, quote: SwapQuote) -> GuardResult:
        c = self.config
        # API returns fractions -> convert to percent to match the config units.
        slippage = quote.prices.slippage * _PCT
        # Cantex charges a pool fee (%) + a network fee (in CC); no admin fee.
        # Guard the pool fee against both the per-pool value and the top-level
        # quote fee percentage, whichever is higher.
        pool_fee = max(self._max_pool_fee(quote), quote.fees.fee_percentage) * _PCT
        network_fee = quote.fees.network_fee.amount  # absolute, in CC

        details = {
            "slippage_pct": slippage,
            "pool_fee_pct": pool_fee,
            "network_fee": network_fee,
        }
        reasons: list[str] = []
        if slippage > c.max_slippage:
            reasons.append(f"slippage {slippage} > {c.max_slippage}")
        if pool_fee > c.max_pool_fee_pct:
            reasons.append(f"pool fee {pool_fee} > {c.max_pool_fee_pct}")
        if network_fee > c.max_network_fee:
            reasons.append(f"network fee {network_fee} > {c.max_network_fee}")

        return GuardResult(ok=not reasons, reasons=reasons, details=details)

    def check(self, quote: SwapQuote) -> GuardResult:
        """Like evaluate() but raises GuardRejected on failure."""
        result = self.evaluate(quote)
        if not result.ok:
            raise GuardRejected(result.reasons)
        return result
