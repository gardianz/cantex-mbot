"""Local SQLite state: swap log, daily counters, and scrape snapshots."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS swaps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    wallet        TEXT NOT NULL,
    direction     TEXT NOT NULL,          -- 'buy' | 'sell'
    sell_symbol   TEXT NOT NULL,
    buy_symbol    TEXT NOT NULL,
    sell_amount   TEXT NOT NULL,
    buy_amount    TEXT NOT NULL,
    admin_fee     TEXT NOT NULL DEFAULT '0',
    liquidity_fee TEXT NOT NULL DEFAULT '0',
    network_fee   TEXT NOT NULL DEFAULT '0',
    price         TEXT NOT NULL DEFAULT '0',
    dry_run       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_swaps_wallet_ts ON swaps (wallet, ts);

CREATE TABLE IF NOT EXISTS daily_counters (
    wallet  TEXT NOT NULL,
    day     TEXT NOT NULL,               -- YYYY-MM-DD (UTC)
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (wallet, day)
);

CREATE TABLE IF NOT EXISTS scrape_snapshots (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    wallet  TEXT NOT NULL,
    kind    TEXT NOT NULL,               -- 'history' | 'activity'
    data    TEXT NOT NULL                -- JSON blob
);
CREATE INDEX IF NOT EXISTS idx_snap_wallet_kind_ts ON scrape_snapshots (wallet, kind, ts);

-- Mirror of the exchange's trading history.
--
-- GET /v1/history/trading hard-caps at the 50 newest rows and ignores both
-- limit and offset (measured 2026-08-21), so the week's loss cannot be read
-- from it at all, and at 50 swaps/day even today's rows start falling off.
-- The sweep runs every 30-60s and a wallet trades far slower than 50 rows per
-- sweep, so mirroring what each poll returns rebuilds the full history locally.
-- The primary key makes re-inserting the same row a no-op.
CREATE TABLE IF NOT EXISTS trades (
    wallet        TEXT NOT NULL,
    ts            REAL NOT NULL,           -- UTC epoch seconds
    day           TEXT NOT NULL,           -- YYYY-MM-DD (UTC)
    input_id      TEXT NOT NULL,
    output_id     TEXT NOT NULL,
    amount_input  TEXT NOT NULL,
    amount_output TEXT NOT NULL,
    pool_cid      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (wallet, ts, pool_cid, input_id, output_id,
                 amount_input, amount_output)
);
CREATE INDEX IF NOT EXISTS idx_trades_wallet_ts ON trades (wallet, ts);
CREATE INDEX IF NOT EXISTS idx_trades_wallet_day ON trades (wallet, day);

CREATE TABLE IF NOT EXISTS fee_obs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    day         TEXT NOT NULL,           -- YYYY-MM-DD (UTC)
    wallet      TEXT NOT NULL,
    pair        TEXT NOT NULL,
    network_fee REAL NOT NULL,           -- CC, absolute
    slippage    REAL NOT NULL DEFAULT 0, -- percent
    pool_fee    REAL NOT NULL DEFAULT 0  -- percent
);
CREATE INDEX IF NOT EXISTS idx_fee_wallet_day ON fee_obs (wallet, day);
CREATE INDEX IF NOT EXISTS idx_fee_pair_day_ts ON fee_obs (pair, day, ts);
CREATE INDEX IF NOT EXISTS idx_fee_wallet_ts ON fee_obs (wallet, ts);
"""


@dataclass(frozen=True)
class SwapRecord:
    wallet: str
    direction: str
    sell_symbol: str
    buy_symbol: str
    sell_amount: Decimal
    buy_amount: Decimal
    admin_fee: Decimal = Decimal(0)
    liquidity_fee: Decimal = Decimal(0)
    network_fee: Decimal = Decimal(0)
    price: Decimal = Decimal(0)
    dry_run: bool = False


def _today() -> str:
    # UTC, so the daily counter resets at 00:00 UTC — the same boundary the web
    # swap count, fee stats and loss windows use (Cantex is UTC). A local date
    # here would reset at local midnight (e.g. WIB = UTC+7, 7h off).
    return datetime.now(timezone.utc).date().isoformat()


class Store:
    """Thread-safe (coarse-lock) SQLite wrapper."""

    def __init__(self, path: str | Path = "state.db") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Migrate older DBs: add fee_obs.slippage / pool_fee if missing.
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(fee_obs)")}
            if "slippage" not in cols:
                self._conn.execute("ALTER TABLE fee_obs ADD COLUMN slippage REAL NOT NULL DEFAULT 0")
            if "pool_fee" not in cols:
                self._conn.execute("ALTER TABLE fee_obs ADD COLUMN pool_fee REAL NOT NULL DEFAULT 0")
            # Normalise legacy pair case once (so queries need no UPPER()), and
            # drop fee rows older than 2 days to keep the table (and its stats
            # query) fast — only today's rows are ever read.
            self._conn.execute(
                "UPDATE fee_obs SET pair = UPPER(pair) WHERE pair <> UPPER(pair)")
            cutoff = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
            self._conn.execute("DELETE FROM fee_obs WHERE day < ?", (cutoff,))
            # The mirror only ever needs back to this week's Monday; keep a
            # generous margin and drop the rest so it cannot grow without bound.
            old_trades = (datetime.now(timezone.utc).date()
                          - timedelta(days=60)).isoformat()
            self._conn.execute("DELETE FROM trades WHERE day < ?", (old_trades,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- swaps ---------------------------------------------------------------

    def record_swap(self, rec: SwapRecord) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO swaps
                   (ts, wallet, direction, sell_symbol, buy_symbol, sell_amount,
                    buy_amount, admin_fee, liquidity_fee, network_fee, price, dry_run)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(), rec.wallet, rec.direction, rec.sell_symbol,
                    rec.buy_symbol, str(rec.sell_amount), str(rec.buy_amount),
                    str(rec.admin_fee), str(rec.liquidity_fee), str(rec.network_fee),
                    str(rec.price), int(rec.dry_run),
                ),
            )
            self._conn.commit()

    def last_buy_cost(
        self, wallet: str, base_symbol: str, token_symbol: str,
    ) -> Decimal | None:
        """Base amount spent on this wallet's most recent live ``base -> token``
        buy, or None if there is no record. Used to measure a round trip's loss
        before selling the token back."""
        with self._lock:
            row = self._conn.execute(
                """SELECT sell_amount FROM swaps
                   WHERE wallet = ? AND dry_run = 0
                     AND UPPER(sell_symbol) = ? AND UPPER(buy_symbol) = ?
                   ORDER BY ts DESC LIMIT 1""",
                (wallet, base_symbol.upper(), token_symbol.upper()),
            ).fetchone()
        return Decimal(row["sell_amount"]) if row else None

    def fees_since(self, wallet: str, seconds: float, *, include_dry: bool = False) -> Decimal:
        """Total fees (admin + liquidity + network) for a wallet in the window."""
        cutoff = time.time() - seconds
        dry_clause = "" if include_dry else "AND dry_run = 0"
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT admin_fee, liquidity_fee, network_fee FROM swaps
                    WHERE wallet = ? AND ts >= ? {dry_clause}""",
                (wallet, cutoff),
            ).fetchall()
        total = Decimal(0)
        for r in rows:
            total += Decimal(r["admin_fee"]) + Decimal(r["liquidity_fee"]) + Decimal(r["network_fee"])
        return total

    # -- daily counters ------------------------------------------------------

    def incr_daily(self, wallet: str, day: str | None = None) -> int:
        day = day or _today()
        with self._lock:
            self._conn.execute(
                """INSERT INTO daily_counters (wallet, day, count) VALUES (?,?,1)
                   ON CONFLICT(wallet, day) DO UPDATE SET count = count + 1""",
                (wallet, day),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT count FROM daily_counters WHERE wallet = ? AND day = ?",
                (wallet, day),
            ).fetchone()
        return int(row["count"])

    def daily_count(self, wallet: str, day: str | None = None) -> int:
        """Swaps counted today. With an explicit ``day`` this reads the legacy
        day-string counter table; the DEFAULT counts today's (UTC) rows in the
        ``swaps`` table by their real ``ts``. The ts path is timezone-proof and
        self-correcting: a row can't be miscounted just because it was written
        with the wrong local date (the pre-UTC-fix bug that left yesterday's
        swaps stuck under today's WIB date)."""
        if day is not None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT count FROM daily_counters WHERE wallet = ? AND day = ?",
                    (wallet, day),
                ).fetchone()
            return int(row["count"]) if row else 0
        start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM swaps WHERE wallet = ? AND ts >= ?",
                (wallet, start),
            ).fetchone()
        return int(row["c"]) if row else 0

    # -- scrape snapshots ----------------------------------------------------

    def save_snapshot(self, wallet: str, kind: str, data: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO scrape_snapshots (ts, wallet, kind, data) VALUES (?,?,?,?)",
                (time.time(), wallet, kind, json.dumps(data)),
            )
            self._conn.commit()

    def latest_snapshot(self, wallet: str, kind: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT data, ts FROM scrape_snapshots
                   WHERE wallet = ? AND kind = ? ORDER BY ts DESC LIMIT 1""",
                (wallet, kind),
            ).fetchone()
        if not row:
            return None
        out = json.loads(row["data"])
        out["_scraped_at"] = datetime.fromtimestamp(row["ts"], tz=timezone.utc).isoformat()
        return out

    # -- mirrored trading history --------------------------------------------

    def record_trades(self, wallet: str, trades) -> int:
        """Mirror the rows a history poll returned. Returns how many were NEW.

        Idempotent: the same row seen by the next poll is ignored, so this can
        be called on every sweep.
        """
        rows = [
            (wallet, t.timestamp.timestamp(),
             t.timestamp.astimezone(timezone.utc).date().isoformat(),
             t.input_id, t.output_id,
             str(t.amount_input), str(t.amount_output), t.pool_cid or "")
            for t in trades
        ]
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                """INSERT OR IGNORE INTO trades
                   (wallet, ts, day, input_id, output_id, amount_input,
                    amount_output, pool_cid)
                   VALUES (?,?,?,?,?,?,?,?)""",
                rows,
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def trades_between(self, wallet: str, start: "date", end: "date") -> list:
        """Mirrored trades for a wallet between two UTC dates, both inclusive,
        oldest first — as ``webclient.Trade`` objects so the loss helpers can
        consume them unchanged."""
        from .webclient import Trade
        with self._lock:
            rows = self._conn.execute(
                """SELECT ts, input_id, output_id, amount_input, amount_output,
                          pool_cid
                   FROM trades WHERE wallet = ? AND day BETWEEN ? AND ?
                   ORDER BY ts""",
                (wallet, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [
            Trade(
                timestamp=datetime.fromtimestamp(r["ts"], tz=timezone.utc),
                input_id=r["input_id"], input_admin="",
                output_id=r["output_id"], output_admin="",
                amount_input=Decimal(r["amount_input"]),
                amount_output=Decimal(r["amount_output"]),
                pool_cid=r["pool_cid"],
            )
            for r in rows
        ]

    def count_trades(self, wallet: str, start: "date", end: "date") -> int:
        """Mirrored trade count between two UTC dates, both inclusive."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM trades "
                "WHERE wallet = ? AND day BETWEEN ? AND ?",
                (wallet, start.isoformat(), end.isoformat()),
            ).fetchone()
        return int(row["c"]) if row else 0

    # -- network-fee observations --------------------------------------------

    def record_fee(
        self, wallet: str, pair: str, network_fee: Decimal,
        slippage: Decimal = Decimal(0), pool_fee: Decimal = Decimal(0),
    ) -> None:
        """Log an observed network fee (CC) plus slippage and pool fee (percent)
        from a quote, for today's per-pair stats."""
        day = datetime.now(timezone.utc).date().isoformat()
        # Normalise case so a pair is one row: base is upper-cased but token
        # symbols keep their source case (USDCx, cETH), which would otherwise
        # split e.g. "USDCX->FRXUSD.B" and "USDCx->FRXUSD.B" into two rows.
        pair = pair.upper()
        with self._lock:
            self._conn.execute(
                "INSERT INTO fee_obs (ts, day, wallet, pair, network_fee, slippage, "
                "pool_fee) VALUES (?,?,?,?,?,?,?)",
                (time.time(), day, wallet, pair, float(network_fee),
                 float(slippage), float(pool_fee)),
            )
            self._conn.commit()

    def latest_fee(self, wallet: str) -> Decimal | None:
        """The most recently observed network fee for a wallet (live quote fee)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT network_fee FROM fee_obs WHERE wallet = ? ORDER BY ts DESC LIMIT 1",
                (wallet,),
            ).fetchone()
        return Decimal(str(row["network_fee"])) if row else None

    def pair_fee_stats(
        self, day: str | None = None
    ) -> list[tuple[str, Decimal, Decimal, Decimal, Decimal, Decimal, int]]:
        """Per-pair fee stats for today (UTC), across all wallets:
        ``(pair, latest_net, min_net, avg_net, latest_slippage, latest_pool_fee,
        count)`` sorted by pair. Network fee is in CC; slippage/pool fee are the
        latest observed values in percent. Pairs differ per pool, so a single
        'FEE now' cannot represent them."""
        day = day or _today()
        with self._lock:
            # Pairs are stored upper-cased (record_fee + a one-time migration),
            # so plain equality here is index-friendly (idx_fee_pair_day_ts).
            rows = self._conn.execute(
                """SELECT pair, MIN(network_fee) AS mn,
                          AVG(network_fee) AS av, COUNT(*) AS n,
                          (SELECT network_fee FROM fee_obs f2
                             WHERE f2.pair = f.pair AND f2.day = f.day
                             ORDER BY ts DESC LIMIT 1) AS latest,
                          (SELECT slippage FROM fee_obs f2
                             WHERE f2.pair = f.pair AND f2.day = f.day
                             ORDER BY ts DESC LIMIT 1) AS slip,
                          (SELECT pool_fee FROM fee_obs f2
                             WHERE f2.pair = f.pair AND f2.day = f.day
                             ORDER BY ts DESC LIMIT 1) AS pool
                   FROM fee_obs f WHERE day = ?
                   GROUP BY pair ORDER BY pair""",
                (day,),
            ).fetchall()
        out: list[tuple[str, Decimal, Decimal, Decimal, Decimal, Decimal, int]] = []
        for r in rows:
            out.append((
                r["pair"], Decimal(str(r["latest"])), Decimal(str(r["mn"])),
                Decimal(str(r["av"])), Decimal(str(r["slip"])),
                Decimal(str(r["pool"])), int(r["n"]),
            ))
        return out

    def fee_stats_today(
        self, wallet: str, day: str | None = None
    ) -> tuple[Decimal | None, Decimal | None, int]:
        """(min, avg, count) of observed network fees for the wallet today (UTC)."""
        day = day or datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            row = self._conn.execute(
                """SELECT MIN(network_fee) AS mn, AVG(network_fee) AS av,
                          COUNT(*) AS n FROM fee_obs
                   WHERE wallet = ? AND day = ?""",
                (wallet, day),
            ).fetchone()
        if not row or not row["n"]:
            return None, None, 0
        mn = Decimal(str(row["mn"]))
        av = Decimal(str(row["av"]))
        return mn, av, int(row["n"])
