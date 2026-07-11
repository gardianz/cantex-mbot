"""Resolve token symbols to InstrumentIds and enumerate USDCX pairs."""
from __future__ import annotations

from dataclasses import dataclass

from cantex_sdk import CantexSDK, InstrumentId, Pool


class MarketError(Exception):
    """Raised when a symbol or pair cannot be resolved."""


@dataclass(frozen=True)
class Pair:
    """A USDCX <-> token pair backed by a specific pool."""

    token: InstrumentId
    token_symbol: str
    usdcx: InstrumentId
    pool_contract_id: str


class MarketMap:
    """Symbol <-> InstrumentId map plus pool enumeration for one account."""

    def __init__(
        self,
        by_symbol: dict[str, InstrumentId],
        by_id: dict[InstrumentId, str],
        pools: list[Pool],
    ) -> None:
        self._by_symbol = by_symbol
        self._by_id = by_id
        self.pools = pools

    @classmethod
    async def build(cls, sdk: CantexSDK) -> "MarketMap":
        admin = await sdk.get_account_admin()
        pools_info = await sdk.get_pool_info()
        by_symbol: dict[str, InstrumentId] = {}
        by_id: dict[InstrumentId, str] = {}
        for inst in admin.instruments:
            by_symbol[inst.instrument_symbol.upper()] = inst.instrument
            by_id[inst.instrument] = inst.instrument_symbol
        return cls(by_symbol, by_id, pools_info.pools)

    def instrument(self, symbol: str) -> InstrumentId:
        try:
            return self._by_symbol[symbol.upper()]
        except KeyError:
            raise MarketError(
                f"Token symbol {symbol!r} not found in account instruments. "
                f"Known: {sorted(self._by_symbol)}"
            ) from None

    def symbol_of(self, instrument: InstrumentId) -> str:
        return self._by_id.get(instrument, instrument.id)

    def usdcx_pairs(self, usdcx_symbol: str = "USDCX") -> list[Pair]:
        """Every pool that has USDCX on one side, as (token, usdcx) pairs."""
        usdcx = self.instrument(usdcx_symbol)
        pairs: list[Pair] = []
        for pool in self.pools:
            if pool.token_a == usdcx:
                token = pool.token_b
            elif pool.token_b == usdcx:
                token = pool.token_a
            else:
                continue
            pairs.append(
                Pair(
                    token=token,
                    token_symbol=self.symbol_of(token),
                    usdcx=usdcx,
                    pool_contract_id=pool.contract_id,
                )
            )
        return pairs

    def pool_token_symbols(self) -> list[str]:
        """Every distinct token symbol that appears in a pool, in pool order."""
        out: list[str] = []
        for pool in self.pools:
            for tok in (pool.token_a, pool.token_b):
                sym = self.symbol_of(tok)
                if sym not in out:
                    out.append(sym)
        return out

    def trade_pairs(
        self,
        base_symbol: str,
        *,
        only_symbols: list[str] | None = None,
        exclude_symbols: tuple[str, ...] = (),
    ) -> list[Pair]:
        """Pairs of ``base`` against every distinct pool token.

        On Cantex every pool is CC(Amulet)<->token, and the swap endpoint routes
        multi-hop (e.g. USDCX->CC->CBTC) at the same fee, so a direct base<->token
        pool is NOT required — any pool token is reachable from the base.

        ``only_symbols`` restricts to a chosen subset; ``exclude_symbols`` drops
        tokens (e.g. the CC pricing token). The base token is always excluded.
        """
        base = self.instrument(base_symbol)
        excl = {s.upper() for s in exclude_symbols} | {base_symbol.upper()}
        only = {s.upper() for s in only_symbols} if only_symbols else None
        pairs: list[Pair] = []
        seen: set[InstrumentId] = set()
        for pool in self.pools:
            for tok in (pool.token_a, pool.token_b):
                sym = self.symbol_of(tok)
                u = sym.upper()
                if u in excl or tok in seen:
                    continue
                if only is not None and u not in only:
                    continue
                seen.add(tok)
                pairs.append(
                    Pair(token=tok, token_symbol=sym, usdcx=base, pool_contract_id="")
                )
        return pairs
