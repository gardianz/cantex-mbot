"""Reader for Cantex data the SDK does not expose (trading history, CC rebates).

Both endpoints accept the SAME Bearer token the SDK derives from the operator
key, so **no browser session cookie is needed** — only that the wallet's SDK has
authenticated:

  * ``GET api.cantex.io/v1/history/trading``       -> JSON list of executed swaps.
    Powers the daily swap target and the dashboard swap counts.
  * ``GET api.cantex.io/v1/account/reward_activity`` -> ``{"rebates": {yesterday,
    this_week, last_week}, "stats": …, "wallet_address": …}``. Rebates are the
    **weekly** CC reward; ``last_week`` carries a ``"Paid: <tx>"`` status once
    settled, ``this_week`` accrues until the week closes.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.cantex.io"

# Async callable returning a fresh Bearer api-key (e.g. ``sdk.authenticate``).
TokenProvider = Callable[[], Awaitable[str]]


class WebClientError(Exception):
    pass


@dataclass(frozen=True)
class Trade:
    timestamp: datetime
    input_id: str
    input_admin: str
    output_id: str
    output_admin: str
    amount_input: Decimal
    amount_output: Decimal
    pool_cid: str


@dataclass(frozen=True)
class Rebates:
    yesterday: Decimal
    this_week: Decimal
    last_week: Decimal
    this_week_status: str = ""
    last_week_status: str = ""


def _parse_ts(raw: str) -> datetime:
    """Parse '2026-07-09 11:59:59.16719+00' into an aware UTC datetime."""
    s = raw.strip().replace(" ", "T")
    # normalise a bare '+00' / '-00' offset to '+00:00'
    if len(s) >= 3 and s[-3] in "+-":
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise WebClientError(f"bad timestamp {raw!r}: {exc}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _dec(v: object) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return Decimal(0)


class WebClient:
    """Per-wallet reader. Bearer token (from the operator key) for everything."""

    def __init__(
        self,
        *,
        token_provider: TokenProvider,
        api_base: str = DEFAULT_API_BASE,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self._token_provider = token_provider
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            from .nethelp import make_connector, make_timeout
            self._session = aiohttp.ClientSession(
                timeout=make_timeout(), connector=make_connector(),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_json(self, path: str, *, retries: int = 2) -> object:
        """GET a JSON endpoint with the Bearer token, retrying transient
        connection timeouts (WSL2/NAT can stall a fresh connection)."""
        token = await self._token_provider()
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "cantex-bot/0.1",
        }
        url = f"{self.api_base}{path}"
        session = await self._get_session()
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with session.get(url, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status in (401, 403):
                        raise WebClientError(
                            f"unauthorised ({resp.status}) — api key expired? {url}"
                        )
                    if resp.status >= 400:
                        raise WebClientError(
                            f"HTTP {resp.status} from {url}: {body[:200]}"
                        )
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise WebClientError(f"{path} not JSON: {exc}") from exc
            except (asyncio.TimeoutError, aiohttp.ClientConnectionError) as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise WebClientError(f"connection failed for {url}: {exc}") from exc
        raise WebClientError(f"connection failed for {url}: {last_exc}")

    # -- trading history -----------------------------------------------------

    @staticmethod
    def _row_to_trade(row: dict) -> Trade:
        return Trade(
            timestamp=_parse_ts(row["timestamp_utc"]),
            input_id=row.get("token_input_instrument_id", ""),
            input_admin=row.get("token_input_instrument_admin", ""),
            output_id=row.get("token_output_instrument_id", ""),
            output_admin=row.get("token_output_instrument_admin", ""),
            amount_input=_dec(row.get("amount_input")),
            amount_output=_dec(row.get("amount_output")),
            pool_cid=row.get("pool_cid", ""),
        )

    async def fetch_trading_history(
        self, *, page_limit: int = 200, max_pages: int = 12, cover_days: int = 8,
    ) -> list[Trade]:
        """Recent trades (newest first), paged until the window is covered.

        The endpoint returns at most one page of the newest trades. The weekly
        loss needs MORE than today's page: at ~50 swaps/day the current week can
        exceed a single page, so if we only read page one the week silently
        "resets" to today as older rows fall off it. We therefore page by
        ``offset`` until a page is short/empty, adds no new rows (an endpoint
        that ignores paging just repeats page one — dedup catches it and stops),
        the oldest row seen predates ``cover_days`` (we only need this week plus
        yesterday), or the page cap is hit."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=cover_days)
        trades: list[Trade] = []
        seen: set[tuple] = set()
        for page in range(max_pages):
            path = (f"/v1/history/trading?limit={page_limit}"
                    f"&offset={page * page_limit}")
            data = await self._get_json(path)
            if not isinstance(data, dict):
                raise WebClientError("history: unexpected payload")
            rows = data.get("history_trading", [])
            new_in_page = 0
            oldest: datetime | None = None
            for row in rows:
                t = self._row_to_trade(row)
                key = (row.get("timestamp_utc", ""), t.pool_cid,
                       str(t.amount_input), str(t.amount_output),
                       t.input_id, t.output_id)
                if key in seen:
                    continue
                seen.add(key)
                trades.append(t)
                new_in_page += 1
                oldest = t.timestamp if oldest is None else min(oldest, t.timestamp)
            # Stop: no fresh rows (paging ignored / history exhausted), a short
            # last page, or we have paged back past the window we care about.
            if new_in_page == 0 or len(rows) < page_limit:
                break
            if oldest is not None and oldest < cutoff:
                break
        return trades

    @staticmethod
    def count_today(trades: list[Trade], *, day: datetime | None = None) -> int:
        """Number of trades whose UTC date equals today (or ``day``)."""
        target = (day or datetime.now(timezone.utc)).date()
        return sum(1 for t in trades if t.timestamp.date() == target)

    @staticmethod
    def count_since(trades: list[Trade], seconds: float) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - seconds
        return sum(1 for t in trades if t.timestamp.timestamp() >= cutoff)

    @staticmethod
    def _loss_over(
        trades: list[Trade], usdcx_symbol: str,
        keep: Callable[[Trade], bool],
    ) -> Decimal:
        """USDCX lost over the COMPLETE buy->sell cycles among the kept trades.

        Loss is measured purely in USDCX: what leaves the wallet on the buy leg
        minus what comes back on the sell leg. Only the USDCX-touching legs
        matter, so this works whether the history records a swap as one direct
        row (``USDCx->CBTC``) or expands the CC hop (``USDCx->CC`` + ``CC->CBTC``)
        — the intermediate CC legs are simply ignored.

        Walking the kept rows in time order:
          * a row whose **input** is USDCX opens a buy — remember the USDCX spent;
          * a row whose **output** is USDCX closes the matching sell — if a buy is
            open, the cycle is COMPLETE: ``loss += spent - received`` (positive =
            loss, negative = gain), and the buy is cleared.

        Incomplete cycles are ignored: a leading sell (``CBTC->USDCx`` whose buy
        fell outside the window → no open buy) is skipped, and a trailing buy
        still holding the token at window end (no closing sell) never counts.
        """
        def sym(s: str) -> str:
            return (s or "").upper()

        u = sym(usdcx_symbol)
        window = sorted((t for t in trades if keep(t)), key=lambda t: t.timestamp)
        loss = Decimal(0)
        spent: Decimal | None = None  # USDCX of an open buy awaiting its sell
        for t in window:
            inp, out = sym(t.input_id), sym(t.output_id)
            if inp == u and out != u:            # USDCX leaves: buy opens
                spent = t.amount_input
            elif out == u and inp != u:          # USDCX returns: sell closes
                if spent is not None:            # ...only if a buy is open
                    loss += spent - t.amount_output
                    spent = None
        return loss

    @staticmethod
    def daily_loss(
        trades: list[Trade], *, usdcx_symbol: str = "USDCX",
        day: datetime | None = None,
    ) -> Decimal:
        """USDCX lost over today's (UTC) complete round-trip cycles.

        The daily reset is UTC-midnight, matching the Cantex web. See
        ``_loss_over`` for the cycle rule.
        """
        target = (day or datetime.now(timezone.utc)).date()
        return WebClient._loss_over(
            trades, usdcx_symbol, lambda t: t.timestamp.date() == target)

    @staticmethod
    def weekly_loss(
        trades: list[Trade], *, usdcx_symbol: str = "USDCX",
        now: datetime | None = None,
    ) -> Decimal:
        """USDCX lost over THIS week's (UTC) complete cycles.

        The week is Monday-00:00 UTC through now — the same Monday-start period
        Cantex uses for rewards. See ``_loss_over`` for the cycle rule.
        """
        today = (now or datetime.now(timezone.utc)).date()
        week_start = today - timedelta(days=today.weekday())   # Monday (UTC)
        return WebClient._loss_over(
            trades, usdcx_symbol, lambda t: t.timestamp.date() >= week_start)

    # -- CC rebates (weekly reward) ------------------------------------------

    async def fetch_rebates(self) -> Rebates:
        data = await self._get_json("/v1/account/reward_activity")
        if not isinstance(data, dict):
            raise WebClientError("reward_activity: unexpected payload")
        reb = data.get("rebates", {})

        def amt(k: str) -> Decimal:
            return _dec(reb.get(k, {}).get("cc_amount"))

        def status(k: str) -> str:
            return str(reb.get(k, {}).get("status", ""))

        return Rebates(
            yesterday=amt("yesterday"),
            this_week=amt("this_week"),
            last_week=amt("last_week"),
            this_week_status=status("this_week"),
            last_week_status=status("last_week"),
        )
