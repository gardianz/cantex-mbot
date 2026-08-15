"""On-chain **fee** reader via the ccview.io Canton explorer.

The Cantex SDK exposes no historical fee data. ccview.io indexes the Canton
ledger and its ``counterparties`` endpoint lists, per party, the CC transferred
to/from each counterparty over a date range. Trading fees are the CC we transfer
OUT to ``cantex.unverified.cns`` (the ANS name of the ``Cantex-validator-1::…``
party). (Rewards/rebates come from the Cantex API instead — see ``webclient``.)

Auth is an anonymous ccview session: ``GET /api/v1/session`` sets an HttpOnly
``sessionId`` cookie which the counterparties endpoint then requires (a bare call
returns 403 SESSION_REQUIRED). No Cantex account cookie is involved — the party
id is public (``AccountInfo.address``), so this needs only the operator key that
already fetched the account info.

Endpoint (verified live 2026-07-10):
  ``GET ccview.io/api/v1/internal/api/v1/parties/counterparties``
      ``?party_id=<addr>&limit=&offset=0&start=YYYY-MM-DD&end=YYYY-MM-DD``
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_CCVIEW_BASE = "https://ccview.io"
FEE_ANS_NAME = "cantex.unverified.cns"


class CCViewError(Exception):
    pass


@dataclass(frozen=True)
class PartyFee:
    fee: Decimal      # CC paid out to cantex.unverified.cns
    start: date
    end: date


def _dec(v: object) -> Decimal:
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _day(value: date | datetime | str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        value = value.date()
    return value.isoformat()


class CCViewClient:
    """Shared, anonymous ccview reader (one session, many parties)."""

    def __init__(self, *, base: str = DEFAULT_CCVIEW_BASE, max_concurrency: int = 3) -> None:
        self.base = base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._authed = False
        # ccview rate-limits aggressively (HTTP 429). Cap our own concurrency so
        # a full portfolio sweep cannot storm it, and back off on any 429.
        self._sem = asyncio.Semaphore(max_concurrency)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            from .nethelp import make_timeout, shared_connector
            self._session = aiohttp.ClientSession(
                timeout=make_timeout(), connector=shared_connector(),
                connector_owner=False,
                headers={
                    "User-Agent": "cantex-bot/0.1",
                    "Referer": self.base + "/",
                },
            )
            self._authed = False
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        session = await self._get_session()
        if self._authed:
            return session
        async with session.get(f"{self.base}/api/v1/session") as resp:
            if resp.status >= 400:
                raise CCViewError(f"ccview session init HTTP {resp.status}")
        self._authed = True
        return session

    async def counterparties(
        self, party_id: str, start: date | datetime | str,
        end: date | datetime | str, *, limit: int = 100,
    ) -> dict:
        url = f"{self.base}/api/v1/internal/api/v1/parties/counterparties"
        params = {
            "party_id": party_id, "limit": str(limit), "offset": "0",
            "start": _day(start), "end": _day(end),
        }
        body = ""
        status = 0
        delay = 1.0
        async with self._sem:
            for attempt in range(4):
                session = await self._ensure_session()
                async with session.get(url, params=params) as resp:
                    body = await resp.text()
                    status = resp.status
                    retry_after = resp.headers.get("Retry-After")
                if status == 403:            # session expired — re-init and retry
                    self._authed = False
                    continue
                if status == 429 and attempt < 3:   # rate-limited — back off
                    wait = (float(retry_after) if retry_after and retry_after.isdigit()
                            else delay)
                    await asyncio.sleep(min(wait, 10.0))
                    delay *= 2
                    continue
                break
        if status >= 400:
            raise CCViewError(f"ccview HTTP {status}: {body[:200]}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise CCViewError(f"ccview not JSON: {exc}") from exc

    async def party_fee(
        self, party_id: str, start: date | datetime | str,
        end: date | datetime | str,
    ) -> PartyFee:
        """Sum trading fees paid (CC out to cantex.unverified.cns) over [start, end]."""
        payload = await self.counterparties(party_id, start, end)
        data = payload.get("data", [])
        ans = payload.get("ans_binding", {})

        def ans_names(cid: str) -> set[str]:
            return {b.get("ans_name", "") for b in ans.get(cid, [])}

        fee = Decimal(0)
        for row in data:
            cid = row.get("counterparty_id", "")
            if FEE_ANS_NAME in ans_names(cid):
                fee += _dec(row.get("transfers_out_volume"))
        return PartyFee(fee=fee, start=_parse_day(start), end=_parse_day(end))


def _parse_day(value: date | datetime | str) -> date:
    if isinstance(value, str):
        return date.fromisoformat(value)
    if isinstance(value, datetime):
        return value.date()
    return value


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def window(days: int) -> tuple[date, date]:
    """A [start, end] date window ending today (UTC), inclusive."""
    end = today_utc()
    return end - timedelta(days=days), end


def fee_windows() -> dict[str, tuple[date, date]]:
    """Date ranges for the fee column: today, yesterday, this week (UTC)."""
    today = today_utc()
    yesterday = today - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())     # Monday
    return {
        "today": (today, today),
        "yesterday": (yesterday, yesterday),
        "this_week": (week_start, today),
    }


def week_windows() -> dict[str, tuple[date, date]]:
    """Date ranges for fee aggregation: today, this week, last week (UTC).

    Weeks are Monday–Sunday, matching Cantex's reward periods.
    """
    today = today_utc()
    week_start = today - timedelta(days=today.weekday())     # Monday
    last_end = week_start - timedelta(days=1)                # Sunday
    last_start = last_end - timedelta(days=6)                # prev Monday
    return {
        "today": (today, today),
        "this_week": (week_start, today),
        "last_week": (last_start, last_end),
    }
