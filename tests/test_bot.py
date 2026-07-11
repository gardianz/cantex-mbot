"""Unit tests for the Cantex bot. No network: the SDK surface is faked."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cantex_sdk import InstrumentId

from cantex_bot.config import GuardConfig, Strategy1Config, TelegramConfig
from cantex_bot.guards import GuardRejected, SwapGuard
from cantex_bot.markets import MarketMap
from cantex_bot.store import Store, SwapRecord
from cantex_bot.swapper import SwapEngine
from cantex_bot.telegram import TelegramNotifier
from cantex_bot.wallets import Wallet
from cantex_bot.config import WalletConfig


# -- fakes -------------------------------------------------------------------

# slippage/pool_fee are API fractions (0.001 = 0.10%); the guard x100s them.
def make_quote(admin="0.001", slippage="0.002", pool_fee="0.001", net="0.1", returned="100"):
    leg = SimpleNamespace(amount=Decimal(net))
    pool = SimpleNamespace(fees=SimpleNamespace(fee_percentage=Decimal(pool_fee)))
    return SimpleNamespace(
        returned=SimpleNamespace(amount=Decimal(returned)),
        returned_amount=Decimal(returned),
        prices=SimpleNamespace(slippage=Decimal(slippage), trade=Decimal("1.5")),
        fees=SimpleNamespace(
            fee_percentage=Decimal(admin),
            amount_admin=Decimal("0.01"),
            amount_liquidity=Decimal("0.02"),
            network_fee=leg,
        ),
        pools=[pool],
    )


def make_event(inp="10", out="100"):
    return SimpleNamespace(
        input_amount=Decimal(inp),
        output_amount=Decimal(out),
        admin_fee_amount=Decimal("0.01"),
        liquidity_fee_amount=Decimal("0.02"),
        price=Decimal("1.5"),
    )


def fake_wallet(sdk) -> Wallet:
    w = Wallet(WalletConfig(name="w1", operator_key="00", trading_key=None), sdk, None)
    w.authed = True  # skip lazy auth in engine tests
    return w


def notifier() -> TelegramNotifier:
    return TelegramNotifier(TelegramConfig(enabled=False))


# -- guards ------------------------------------------------------------------

def test_guard_passes_within_limits():
    g = SwapGuard(GuardConfig())
    res = g.evaluate(make_quote())
    assert res.ok and not res.reasons


def test_guard_rejects_high_slippage():
    g = SwapGuard(GuardConfig(max_slippage=Decimal("0.1")))
    res = g.evaluate(make_quote(slippage="5"))
    assert not res.ok
    assert any("slippage" in r for r in res.reasons)


def test_guard_check_raises():
    g = SwapGuard(GuardConfig(max_pool_fee_pct=Decimal("0.01")))
    with pytest.raises(GuardRejected):
        g.check(make_quote(pool_fee="9"))


def test_guard_uses_max_pool_fee():
    quote = make_quote()
    quote.pools = [
        SimpleNamespace(fees=SimpleNamespace(fee_percentage=Decimal("0.1"))),
        SimpleNamespace(fees=SimpleNamespace(fee_percentage=Decimal("0.9"))),
    ]
    g = SwapGuard(GuardConfig(max_pool_fee_pct=Decimal("0.5")))
    res = g.evaluate(quote)
    assert not res.ok and any("pool fee" in r for r in res.reasons)


# -- store -------------------------------------------------------------------

def test_store_fees_and_counters(tmp_path):
    store = Store(tmp_path / "s.db")
    store.record_swap(SwapRecord(
        wallet="w1", direction="buy", sell_symbol="USDCX", buy_symbol="cBTC",
        sell_amount=Decimal("10"), buy_amount=Decimal("1"),
        admin_fee=Decimal("0.1"), liquidity_fee=Decimal("0.2"), network_fee=Decimal("0.3"),
    ))
    assert store.fees_since("w1", 3600) == Decimal("0.6")
    assert store.fees_since("w2", 3600) == Decimal("0")
    # default daily_count is ts-based: one real swap recorded above -> 1 today
    assert store.daily_count("w1") == 1
    # legacy day-string counter still works via an explicit day
    assert store.incr_daily("w1", day="2026-07-11") == 1
    assert store.incr_daily("w1", day="2026-07-11") == 2
    assert store.daily_count("w1", day="2026-07-11") == 2
    store.close()


def test_daily_count_ts_based_ignores_stale_label(tmp_path):
    """A swap whose real ts is yesterday (UTC) must NOT count today, even when a
    legacy daily_counters row was mislabeled under today's date (the WIB bug)."""
    import time as _t
    store = Store(tmp_path / "s.db")
    today = datetime.now(timezone.utc).date().isoformat()
    store.incr_daily("w1", day=today)                       # legacy mislabeled row = 1
    store._conn.execute(                                    # real swap ts = ~26h ago
        "INSERT INTO swaps (ts, wallet, direction, sell_symbol, buy_symbol, "
        "sell_amount, buy_amount) VALUES (?,?,?,?,?,?,?)",
        (_t.time() - 26 * 3600, "w1", "buy", "USDCX", "CBTC", "1", "1"))
    store._conn.commit()
    assert store.daily_count("w1") == 0                     # ts-based: nothing today
    assert store.daily_count("w1", day=today) == 1          # legacy label still 1
    store.close()


def test_store_dry_fees_excluded(tmp_path):
    store = Store(tmp_path / "s.db")
    store.record_swap(SwapRecord(
        wallet="w1", direction="buy", sell_symbol="USDCX", buy_symbol="cBTC",
        sell_amount=Decimal("10"), buy_amount=Decimal("1"),
        admin_fee=Decimal("1"), dry_run=True,
    ))
    assert store.fees_since("w1", 3600) == Decimal("0")
    assert store.fees_since("w1", 3600, include_dry=True) == Decimal("1")
    store.close()


def test_store_fee_stats(tmp_path):
    store = Store(tmp_path / "s.db")
    for f in ("0.80", "0.70", "0.90"):
        store.record_fee("w1", "USDCX->CBTC", Decimal(f))
    mn, av, n = store.fee_stats_today("w1")
    assert n == 3
    assert mn == Decimal("0.7")
    assert abs(av - Decimal("0.8")) < Decimal("0.0001")  # (0.8+0.7+0.9)/3, float avg
    assert store.fee_stats_today("w2") == (None, None, 0)
    store.close()


def test_strategy_poll_interval_adaptive(tmp_path):
    from types import SimpleNamespace
    from cantex_bot.strategies.strategy1 import Strategy1
    from cantex_bot.config import Strategy1Config
    store = Store(tmp_path / "s.db")
    engine = SwapEngine(SwapGuard(GuardConfig(max_network_fee=Decimal("0.7"))),
                        store, notifier(), dry_run=True)
    s1cfg = Strategy1Config(poll_min_seconds=0.5, poll_max_seconds=5.0, poll_far_ratio=0.3)
    strat = Strategy1(SimpleNamespace(wallets={}, names=[]), engine, s1cfg, notifier(), store)

    def out(fee):
        g = None if fee is None else SimpleNamespace(details={"network_fee": Decimal(str(fee))})
        return SimpleNamespace(guard=g)

    assert strat._poll_interval(out("0.7")) == 0.5      # at threshold -> min
    assert strat._poll_interval(out("0.6")) == 0.5      # below -> min
    assert strat._poll_interval(out("0.91")) == 5.0     # 30% above -> max
    assert strat._poll_interval(out(None)) == 5.0       # unknown -> max
    mid = strat._poll_interval(out("0.805"))            # 15% above -> ~half
    assert 2.5 < mid < 3.0
    store.close()


@pytest.mark.asyncio
async def test_strategy_sells_held_token_first(tmp_path, monkeypatch):
    """Resume/round-trip: when a token balance is held, the first action is a
    SELL (not a buy). Covers restart-continue behaviour."""
    import asyncio as _a
    from cantex_bot.strategies import strategy1 as s1mod
    from cantex_bot.markets import Pair
    from cantex_bot.config import Strategy1Config

    usdcx = InstrumentId("a", "USDCX"); cbtc = InstrumentId("a", "CBTC")

    class FakeMarket:
        def instrument(self, sym):
            return {"USDCX": usdcx, "CC": InstrumentId("a", "CC"), "CBTC": cbtc}[sym.upper()]
        def trade_pairs(self, base, only_symbols=None, exclude_symbols=()):
            return [Pair(token=cbtc, token_symbol="CBTC", usdcx=usdcx, pool_contract_id="")]

    monkeypatch.setattr(s1mod.MarketMap, "build", AsyncMock(return_value=FakeMarket()))

    store = Store(tmp_path / "s.db")
    outcome = SimpleNamespace(counted=True, ok=True, error=None, reject_reasons=None,
                              buy_amount=Decimal("1"), buy_symbol="USDCX", guard=None)
    engine = SimpleNamespace(
        dry_run=True,
        execute_swap=AsyncMock(return_value=outcome),
        guard=SimpleNamespace(config=SimpleNamespace(max_network_fee=Decimal("0.7"))))
    cfg = Strategy1Config(daily_swap_target=2, cooldown_seconds=0)
    strat = s1mod.Strategy1(SimpleNamespace(wallets={}, names=[]), engine, cfg,
                            notifier(), store, tokens=["CBTC"])
    strat._web_swaps_today = AsyncMock(return_value=0)
    strat._price_cc_in_usdcx = AsyncMock(return_value=Decimal("10"))
    strat._token_cc_value = AsyncMock(return_value=Decimal("110"))   # >= min ticket
    strat._balances = AsyncMock(return_value=(Decimal("0.0002"), Decimal("100"), Decimal("50")))
    strat._wait_next_day = AsyncMock(return_value=False)  # end on target instead of idling

    wallet = SimpleNamespace(name="w1", ensure_auth=AsyncMock(),
                             sdk=SimpleNamespace())
    await strat._run_wallet(wallet, _a.Event())

    assert engine.execute_swap.await_args_list, "no swap attempted"
    first = engine.execute_swap.await_args_list[0].kwargs
    assert first["direction"] == "sell" and first["sell_symbol"] == "CBTC"
    store.close()


@pytest.mark.asyncio
async def test_strategy_buys_when_flat(tmp_path, monkeypatch):
    """With no token held, the action is a BUY (USDCX -> token)."""
    import asyncio as _a
    from cantex_bot.strategies import strategy1 as s1mod
    from cantex_bot.markets import Pair
    from cantex_bot.config import Strategy1Config

    usdcx = InstrumentId("a", "USDCX"); cbtc = InstrumentId("a", "CBTC")

    class FakeMarket:
        def instrument(self, sym):
            return {"USDCX": usdcx, "CC": InstrumentId("a", "CC"), "CBTC": cbtc}[sym.upper()]
        def trade_pairs(self, base, only_symbols=None, exclude_symbols=()):
            return [Pair(token=cbtc, token_symbol="CBTC", usdcx=usdcx, pool_contract_id="")]

    monkeypatch.setattr(s1mod.MarketMap, "build", AsyncMock(return_value=FakeMarket()))
    store = Store(tmp_path / "s.db")
    outcome = SimpleNamespace(counted=True, ok=True, error=None, reject_reasons=None,
                              buy_amount=Decimal("1"), buy_symbol="CBTC", guard=None)
    engine = SimpleNamespace(
        dry_run=True, execute_swap=AsyncMock(return_value=outcome),
        guard=SimpleNamespace(config=SimpleNamespace(max_network_fee=Decimal("0.7"))))
    cfg = Strategy1Config(daily_swap_target=1, cooldown_seconds=0)
    strat = s1mod.Strategy1(SimpleNamespace(wallets={}, names=[]), engine, cfg,
                            notifier(), store, tokens=["CBTC"])
    strat._web_swaps_today = AsyncMock(return_value=0)
    strat._price_cc_in_usdcx = AsyncMock(return_value=Decimal("10"))
    strat._token_cc_value = AsyncMock(return_value=Decimal("0"))
    strat._balances = AsyncMock(return_value=(Decimal("0"), Decimal("100"), Decimal("50")))  # flat
    strat._wait_next_day = AsyncMock(return_value=False)
    await strat._run_wallet(SimpleNamespace(name="w1", ensure_auth=AsyncMock(),
                                            sdk=SimpleNamespace()), _a.Event())
    first = engine.execute_swap.await_args_list[0].kwargs
    assert first["direction"] == "buy" and first["sell_symbol"] == "USDCX"
    store.close()


@pytest.mark.asyncio
async def test_strategy_stops_on_too_small(tmp_path, monkeypatch):
    """A 'Too small amount' (dust) error is treated as saldo-kurang and stops the
    wallet after insufficient_retries — not as a repeated-failure abort."""
    import asyncio as _a
    from cantex_bot.strategies import strategy1 as s1mod
    from cantex_bot.markets import Pair
    from cantex_bot.config import Strategy1Config
    from cantex_bot.runstate import RunState, STOPPED

    usdcx = InstrumentId("a", "USDCX"); cbtc = InstrumentId("a", "CBTC")

    class FakeMarket:
        def instrument(self, sym):
            return {"USDCX": usdcx, "CC": InstrumentId("a", "CC"), "CBTC": cbtc}[sym.upper()]
        def trade_pairs(self, base, only_symbols=None, exclude_symbols=()):
            return [Pair(token=cbtc, token_symbol="CBTC", usdcx=usdcx, pool_contract_id="")]

    monkeypatch.setattr(s1mod.MarketMap, "build", AsyncMock(return_value=FakeMarket()))
    store = Store(tmp_path / "s.db")
    dust_fail = SimpleNamespace(
        counted=False, ok=False, error="API error 400: Too small amount",
        reject_reasons=None, buy_amount=Decimal("0"), buy_symbol="USDCX", guard=None)
    engine = SimpleNamespace(
        dry_run=False, execute_swap=AsyncMock(return_value=dust_fail),
        guard=SimpleNamespace(config=SimpleNamespace(max_network_fee=Decimal("0.7"))))
    cfg = Strategy1Config(daily_swap_target=99, insufficient_retries=3, cooldown_seconds=0)
    rs = RunState(); rs.begin(["w1"], ["CBTC"])
    strat = s1mod.Strategy1(SimpleNamespace(wallets={}, names=["w1"]), engine, cfg,
                            notifier(), store, run_state=rs, tokens=["CBTC"])
    strat._web_swaps_today = AsyncMock(return_value=0)
    strat._price_cc_in_usdcx = AsyncMock(return_value=Decimal("10"))
    strat._token_cc_value = AsyncMock(return_value=Decimal("110"))   # sellable -> sell attempted
    strat._balances = AsyncMock(return_value=(Decimal("0.02"), Decimal("0"), Decimal("50")))
    strat._wait_next_day = AsyncMock(return_value=False)
    await strat._run_wallet(SimpleNamespace(name="w1", ensure_auth=AsyncMock(),
                                            sdk=SimpleNamespace()), _a.Event())
    # stopped as saldo-kurang, and it did NOT spin up to the 6-failure abort.
    assert rs.view("w1").status == STOPPED
    assert rs.view("w1").plan == "saldo kurang"
    assert engine.execute_swap.await_count == cfg.insufficient_retries
    store.close()


def _strategy_fixture(tmp_path, monkeypatch, *, retries=3):
    """Common Strategy1 setup for the dust/afford tests. Returns (strat, engine, rs)."""
    from cantex_bot.strategies import strategy1 as s1mod
    from cantex_bot.markets import Pair
    from cantex_bot.config import Strategy1Config
    from cantex_bot.runstate import RunState

    usdcx = InstrumentId("a", "USDCX"); cbtc = InstrumentId("a", "CBTC")

    class FakeMarket:
        def instrument(self, sym):
            return {"USDCX": usdcx, "CC": InstrumentId("a", "CC"), "CBTC": cbtc}[sym.upper()]
        def trade_pairs(self, base, only_symbols=None, exclude_symbols=()):
            return [Pair(token=cbtc, token_symbol="CBTC", usdcx=usdcx, pool_contract_id="")]

    monkeypatch.setattr(s1mod.MarketMap, "build", AsyncMock(return_value=FakeMarket()))
    store = Store(tmp_path / "s.db")
    engine = SimpleNamespace(
        dry_run=False, execute_swap=AsyncMock(),
        guard=SimpleNamespace(config=SimpleNamespace(max_network_fee=Decimal("0.7"))))
    cfg = Strategy1Config(daily_swap_target=99, insufficient_retries=retries, cooldown_seconds=0)
    rs = RunState(); rs.begin(["w1"], ["CBTC"])
    strat = s1mod.Strategy1(SimpleNamespace(wallets={}, names=["w1"]), engine, cfg,
                            notifier(), store, run_state=rs, tokens=["CBTC"])
    strat._web_swaps_today = AsyncMock(return_value=0)
    strat._price_cc_in_usdcx = AsyncMock(return_value=Decimal("10"))
    # Terminal states now idle until the next UTC day; in tests return False
    # (as if stopped) so _run_wallet ends instead of sleeping for hours.
    strat._wait_next_day = AsyncMock(return_value=False)
    return strat, engine, rs, store


@pytest.mark.asyncio
async def test_strategy_dust_holding_not_sold(tmp_path, monkeypatch):
    """w2 case: dust CBTC (unsellable), no USDCX/CC -> never swaps, stops saldo kurang."""
    from cantex_bot.runstate import STOPPED
    import asyncio as _a
    strat, engine, rs, store = _strategy_fixture(tmp_path, monkeypatch, retries=3)
    strat._token_cc_value = AsyncMock(return_value=Decimal("0.001"))  # dust < 10 CC
    strat._balances = AsyncMock(return_value=(Decimal("0.00000044"), Decimal("0"), Decimal("0")))
    await strat._run_wallet(SimpleNamespace(name="w1", ensure_auth=AsyncMock(),
                                            sdk=SimpleNamespace()), _a.Event())
    assert engine.execute_swap.await_count == 0            # dust not sold, no USDCX to buy
    assert rs.view("w1").status == STOPPED and rs.view("w1").plan == "saldo kurang"
    store.close()


@pytest.mark.asyncio
async def test_strategy_cant_afford_fee_stops(tmp_path, monkeypatch):
    """Guard-rejected on fee but wallet has no CC to pay it -> saldo kurang (not endless poll)."""
    from cantex_bot.runstate import STOPPED
    import asyncio as _a
    strat, engine, rs, store = _strategy_fixture(tmp_path, monkeypatch, retries=3)
    engine.execute_swap = AsyncMock(return_value=SimpleNamespace(
        counted=False, ok=False, error=None, reject_reasons=["network fee 0.77 > 0.7"],
        guard=SimpleNamespace(details={"network_fee": Decimal("0.77")}),
        buy_amount=Decimal(0), buy_symbol="USDCX"))
    strat._token_cc_value = AsyncMock(return_value=Decimal("110"))   # sellable
    strat._balances = AsyncMock(return_value=(Decimal("0.02"), Decimal("0"), Decimal("0")))  # cc=0
    await strat._run_wallet(SimpleNamespace(name="w1", ensure_auth=AsyncMock(),
                                            sdk=SimpleNamespace()), _a.Event())
    assert rs.view("w1").status == STOPPED and rs.view("w1").plan == "saldo kurang"
    assert engine.execute_swap.await_count == 3          # stops after insufficient_retries
    store.close()


@pytest.mark.asyncio
async def test_strategy_confirms_ambiguous_swap_via_history(tmp_path, monkeypatch):
    """Anti-collision: a submitted swap whose confirmation errors is reconciled
    against the trading history. If the count went up it counts as success, so the
    bot never fires the opposite leg on a pending swap."""
    from cantex_bot.runstate import DONE
    import asyncio as _a
    strat, engine, rs, store = _strategy_fixture(tmp_path, monkeypatch, retries=3)
    engine.dry_run = False
    engine.execute_swap = AsyncMock(return_value=SimpleNamespace(
        counted=False, ok=False, error="swap failed: confirm timeout",
        submitted_attempt=True, reject_reasons=None, guard=None,
        buy_amount=Decimal("0"), buy_symbol="CBTC"))
    strat.config = Strategy1Config(daily_swap_target=1, insufficient_retries=3,
                                   cooldown_seconds=0)
    strat._token_cc_value = AsyncMock(return_value=Decimal("0"))          # flat -> buy
    strat._balances = AsyncMock(return_value=(Decimal("0"), Decimal("100"), Decimal("50")))
    strat._confirm_via_history = AsyncMock(return_value=True)             # history: it settled
    await strat._run_wallet(SimpleNamespace(name="w1", ensure_auth=AsyncMock(),
                                            sdk=SimpleNamespace()), _a.Event())
    # one submit, reconciled as done — reached target, NOT a repeated-failure abort.
    assert engine.execute_swap.await_count == 1
    assert rs.view("w1").status == DONE
    strat._confirm_via_history.assert_awaited()
    store.close()


@pytest.mark.asyncio
async def test_strategy_unconfirmed_swap_is_failure(tmp_path, monkeypatch):
    """If the history does NOT confirm the ambiguous swap it is a real failure
    (retry same leg, never a silent flip). Repeated unconfirmed swaps count toward
    the abort cap and stop the wallet — they are not treated as success."""
    from cantex_bot.runstate import STOPPED
    import asyncio as _a
    strat, engine, rs, store = _strategy_fixture(tmp_path, monkeypatch, retries=99)
    engine.dry_run = False
    engine.execute_swap = AsyncMock(return_value=SimpleNamespace(
        counted=False, ok=False, error="swap failed: confirm timeout",
        submitted_attempt=True, reject_reasons=None, guard=None,
        buy_amount=Decimal("0"), buy_symbol="CBTC"))
    strat.config = Strategy1Config(daily_swap_target=99, insufficient_retries=99,
                                   cooldown_seconds=0)
    strat._token_cc_value = AsyncMock(return_value=Decimal("0"))
    strat._balances = AsyncMock(return_value=(Decimal("0"), Decimal("100"), Decimal("50")))
    strat._confirm_via_history = AsyncMock(return_value=False)  # history never confirms
    await strat._run_wallet(SimpleNamespace(name="w1", ensure_auth=AsyncMock(),
                                            sdk=SimpleNamespace()), _a.Event())
    # single pair -> abort cap = max(6, 2) = 6 consecutive failures, then stop.
    assert engine.execute_swap.await_count == 6
    assert rs.view("w1").status == STOPPED
    assert rs.view("w1").plan == "stopped: repeated errors"
    store.close()


def test_seconds_until_next_midnight_is_utc():
    """Daily reset must count down to 00:00 UTC, not local midnight (WIB=UTC+7
    would be ~7h off)."""
    from datetime import timedelta
    from cantex_bot.scheduler import _seconds_until_next_midnight
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    expected = (nxt - now).total_seconds()      # computed in UTC
    got = _seconds_until_next_midnight()
    assert 0 < got <= 86400
    assert abs(got - expected) < 2              # matches UTC, not a local offset


def test_store_daily_counter_uses_utc(tmp_path, monkeypatch):
    """The daily counter key is the UTC date, so it resets at 00:00 UTC."""
    from datetime import timezone as _tz
    store = Store(tmp_path / "s.db")
    utc_day = datetime.now(_tz.utc).date().isoformat()
    store.incr_daily("w1")
    assert store.daily_count("w1", day=utc_day) == 1   # stored under the UTC date
    store.close()


@pytest.mark.asyncio
async def test_strategy_resets_daily_count_on_utc_rollover(tmp_path, monkeypatch):
    """Across 00:00 UTC the daily swap count resets to 0 (session + web baseline),
    so a wallet that hit target yesterday trades again today (fixes 17/50 stuck)."""
    import asyncio as _a
    from datetime import date, datetime as _dt
    from cantex_bot.strategies import strategy1 as s1mod
    strat, engine, rs, store = _strategy_fixture(tmp_path, monkeypatch, retries=99)
    engine.dry_run = True
    engine.execute_swap = AsyncMock(return_value=SimpleNamespace(
        counted=True, ok=True, error=None, reject_reasons=None, guard=None,
        buy_amount=Decimal("1"), buy_symbol="CBTC"))
    strat.config = Strategy1Config(daily_swap_target=1, insufficient_retries=99,
                                   cooldown_seconds=0)
    strat._token_cc_value = AsyncMock(return_value=Decimal("0"))          # flat -> buy
    strat._balances = AsyncMock(return_value=(Decimal("0"), Decimal("100"), Decimal("50")))
    strat._web_swaps_today = AsyncMock(return_value=0)                    # web lags -> ride session

    state = {"day": date(2026, 7, 11)}

    class FakeDT:
        @staticmethod
        def now(tz=None):
            d = state["day"]
            return _dt(d.year, d.month, d.day, 12, tzinfo=tz)

    monkeypatch.setattr(s1mod, "datetime", FakeDT)

    waits = iter([True, False])   # 1st terminal -> new day; 2nd -> stop

    async def _wait(stop):
        r = next(waits)
        if r:
            state["day"] = date(2026, 7, 12)   # advance one UTC day
        return r

    strat._wait_next_day = _wait
    await strat._run_wallet(SimpleNamespace(name="w1", ensure_auth=AsyncMock(),
                                            sdk=SimpleNamespace()), _a.Event())
    # day1: 1 swap -> target; rollover resets; day2: 1 swap -> target. 2 total.
    assert engine.execute_swap.await_count == 2
    store.close()


def test_store_snapshot(tmp_path):
    store = Store(tmp_path / "s.db")
    store.save_snapshot("w1", "activity", {"cc_rebate_candidates": {"yesterday.cc": 5}})
    snap = store.latest_snapshot("w1", "activity")
    assert snap["cc_rebate_candidates"]["yesterday.cc"] == 5
    assert "_scraped_at" in snap
    store.close()


# -- markets -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_marketmap_pairs():
    usdcx = InstrumentId(admin="DSO", id="USDCX")
    cbtc = InstrumentId(admin="DSO", id="cBTC")
    cc = InstrumentId(admin="DSO", id="CC")
    admin = SimpleNamespace(instruments=[
        SimpleNamespace(instrument=usdcx, instrument_symbol="USDCX"),
        SimpleNamespace(instrument=cbtc, instrument_symbol="cBTC"),
        SimpleNamespace(instrument=cc, instrument_symbol="CC"),
    ])
    pools = SimpleNamespace(pools=[
        SimpleNamespace(contract_id="p1", token_a=usdcx, token_b=cbtc),
        SimpleNamespace(contract_id="p2", token_a=cc, token_b=usdcx),
        SimpleNamespace(contract_id="p3", token_a=cbtc, token_b=cc),  # no usdcx
    ])
    sdk = SimpleNamespace(
        get_account_admin=AsyncMock(return_value=admin),
        get_pool_info=AsyncMock(return_value=pools),
    )
    mm = await MarketMap.build(sdk)
    assert mm.instrument("usdcx") == usdcx
    pairs = mm.usdcx_pairs("USDCX")
    tokens = {p.token_symbol for p in pairs}
    assert tokens == {"cBTC", "CC"}  # both pools with USDCX


@pytest.mark.asyncio
async def test_trade_pairs_cc_centric():
    """Cantex reality: every pool is CC<->token; USDCX reaches tokens via routing."""
    usdcx = InstrumentId(admin="usdc", id="USDCx")
    cc = InstrumentId(admin="DSO", id="Amulet")
    cbtc = InstrumentId(admin="cbtc", id="CBTC")
    ceth = InstrumentId(admin="ceth", id="cETH")
    admin = SimpleNamespace(instruments=[
        SimpleNamespace(instrument=usdcx, instrument_symbol="USDCX"),
        SimpleNamespace(instrument=cc, instrument_symbol="CC"),
        SimpleNamespace(instrument=cbtc, instrument_symbol="CBTC"),
        SimpleNamespace(instrument=ceth, instrument_symbol="cETH"),
    ])
    pools = SimpleNamespace(pools=[  # all CC-paired, NO direct USDCX<->token pool
        SimpleNamespace(contract_id="p1", token_a=cc, token_b=usdcx),
        SimpleNamespace(contract_id="p2", token_a=cc, token_b=cbtc),
        SimpleNamespace(contract_id="p3", token_a=cc, token_b=ceth),
    ])
    sdk = SimpleNamespace(
        get_account_admin=AsyncMock(return_value=admin),
        get_pool_info=AsyncMock(return_value=pools),
    )
    mm = await MarketMap.build(sdk)
    # base USDCX, exclude CC -> the routable tokens (not USDCX, not CC)
    got = [p.token_symbol for p in mm.trade_pairs("USDCX", exclude_symbols=("CC",))]
    assert got == ["CBTC", "cETH"]
    # subset filter
    subset = [p.token_symbol for p in
              mm.trade_pairs("USDCX", only_symbols=["cETH"], exclude_symbols=("CC",))]
    assert subset == ["cETH"]
    # base token is always dropped
    assert all(p.token != usdcx for p in mm.trade_pairs("USDCX", exclude_symbols=("CC",)))


# -- swapper -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_engine_dry_run_counts(tmp_path):
    store = Store(tmp_path / "s.db")
    sdk = SimpleNamespace(get_swap_quote=AsyncMock(return_value=make_quote()))
    wallet = fake_wallet(sdk)
    engine = SwapEngine(SwapGuard(GuardConfig()), store, notifier(), dry_run=True)
    out = await engine.execute_swap(
        wallet, sell=InstrumentId("DSO", "USDCX"), buy=InstrumentId("DSO", "cBTC"),
        sell_amount=Decimal("10"), sell_symbol="USDCX", buy_symbol="cBTC", direction="buy",
    )
    assert out.dry_run and out.counted and not out.executed
    assert store.daily_count("w1") == 1
    store.close()


@pytest.mark.asyncio
async def test_engine_guard_reject_no_count(tmp_path):
    store = Store(tmp_path / "s.db")
    sdk = SimpleNamespace(get_swap_quote=AsyncMock(return_value=make_quote(slippage="99")))
    wallet = fake_wallet(sdk)
    engine = SwapEngine(SwapGuard(GuardConfig(max_slippage=Decimal("1"))), store, notifier(), dry_run=True)
    out = await engine.execute_swap(
        wallet, sell=InstrumentId("DSO", "USDCX"), buy=InstrumentId("DSO", "cBTC"),
        sell_amount=Decimal("10"), sell_symbol="USDCX", buy_symbol="cBTC", direction="buy",
    )
    assert out.reject_reasons and not out.counted
    assert store.daily_count("w1") == 0
    store.close()


@pytest.mark.asyncio
async def test_guard_reject_is_not_an_error(tmp_path):
    """Polling mode: a guard reject is 'wait', not a failure (error stays None),
    so Strategy1 does not count it toward the abort cap."""
    store = Store(tmp_path / "s.db")
    sdk = SimpleNamespace(get_swap_quote=AsyncMock(return_value=make_quote(net="9")))
    wallet = fake_wallet(sdk)
    engine = SwapEngine(SwapGuard(GuardConfig(max_network_fee=Decimal("0.7"))),
                        store, notifier(), dry_run=True)
    out = await engine.execute_swap(
        wallet, sell=InstrumentId("DSO", "USDCX"), buy=InstrumentId("DSO", "CBTC"),
        sell_amount=Decimal("10"), sell_symbol="USDCX", buy_symbol="CBTC",
        direction="buy", quiet_reject=True,
    )
    assert out.reject_reasons and out.error is None and not out.counted
    store.close()


@pytest.mark.asyncio
async def test_engine_live_executes(tmp_path):
    store = Store(tmp_path / "s.db")
    sdk = SimpleNamespace(
        get_swap_quote=AsyncMock(return_value=make_quote()),
        swap_and_confirm=AsyncMock(return_value=make_event()),
    )
    wallet = fake_wallet(sdk)
    engine = SwapEngine(SwapGuard(GuardConfig()), store, notifier(), dry_run=False)
    out = await engine.execute_swap(
        wallet, sell=InstrumentId("DSO", "USDCX"), buy=InstrumentId("DSO", "cBTC"),
        sell_amount=Decimal("10"), sell_symbol="USDCX", buy_symbol="cBTC", direction="buy",
    )
    assert out.executed and out.buy_amount == Decimal("100")
    assert store.fees_since("w1", 3600) == Decimal("0.13")  # 0.01+0.02+0.10
    store.close()


# -- webclient ---------------------------------------------------------------

from datetime import datetime, timedelta, timezone

from cantex_bot.webclient import Rebates, Trade, WebClient, WebClientError, _parse_ts


def test_parse_ts_bare_offset():
    dt = _parse_ts("2026-07-09 11:59:59.16719+00")
    assert dt.tzinfo is not None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 7, 9, 11)


def test_parse_ts_full_offset():
    dt = _parse_ts("2026-07-09T10:00:00+02:00")
    assert dt.astimezone(timezone.utc).hour == 8  # normalised to UTC


def test_parse_ts_bad_raises():
    with pytest.raises(WebClientError):
        _parse_ts("not-a-date")


def _trade(dt: datetime) -> Trade:
    return Trade(
        timestamp=dt, input_id="USDCX", input_admin="a", output_id="cBTC",
        output_admin="b", amount_input=Decimal("1"), amount_output=Decimal("2"),
        pool_cid="p1",
    )


def _leg(dt: datetime, inp: str, out: str, ain: str, aout: str) -> Trade:
    return Trade(timestamp=dt, input_id=inp, input_admin="a", output_id=out,
                 output_admin="b", amount_input=Decimal(ain),
                 amount_output=Decimal(aout), pool_cid="p")


def _cycle(base: datetime, token: str, spent: str, got: str) -> list[Trade]:
    """Four legs of one round trip: USDCx->CC->token->CC->USDCx."""
    from datetime import timedelta
    return [
        _leg(base + timedelta(seconds=0), "USDCx", "CC", spent, "1"),
        _leg(base + timedelta(seconds=1), "CC", token, "1", "0.5"),
        _leg(base + timedelta(seconds=2), token, "CC", "0.5", "1"),
        _leg(base + timedelta(seconds=3), "CC", "USDCx", "1", got),
    ]


def test_daily_loss_complete_cycle():
    now = datetime.now(timezone.utc).replace(hour=12)
    trades = _cycle(now, "CBTC", spent="100", got="99.2")
    # loss = input(USDCx->CC) - output(CC->USDCx) = 100 - 99.2
    assert WebClient.daily_loss(trades) == Decimal("0.8")


def test_daily_loss_sums_multiple_cycles_and_gain():
    from datetime import timedelta
    now = datetime.now(timezone.utc).replace(hour=8)
    trades = (_cycle(now, "CBTC", "100", "99")            # loss 1.0
              + _cycle(now + timedelta(minutes=1), "cETH", "50", "51"))  # gain -1.0
    assert WebClient.daily_loss(trades) == Decimal("0.0")


def test_daily_loss_ignores_incomplete_cycle():
    from datetime import timedelta
    now = datetime.now(timezone.utc).replace(hour=10)
    # bought but never sold back: only USDCx->CC, CC->token
    partial = [
        _leg(now, "USDCx", "CC", "100", "1"),
        _leg(now + timedelta(seconds=1), "CC", "CBTC", "1", "0.5"),
    ]
    assert WebClient.daily_loss(partial) == Decimal("0")


def test_daily_loss_only_counts_today():
    from datetime import timedelta
    yesterday = datetime.now(timezone.utc).replace(hour=12) - timedelta(days=1)
    assert WebClient.daily_loss(_cycle(yesterday, "CBTC", "100", "90")) == Decimal("0")


def test_daily_loss_direct_two_row_format():
    """Real history collapses the CC hop: a round trip is two rows
    (USDCx->CBTC, CBTC->USDCx). Loss = USDCx spent - USDCx returned."""
    from datetime import timedelta
    now = datetime.now(timezone.utc).replace(hour=9)
    trades = [
        _leg(now, "USDCx", "CBTC", "100", "0.001"),
        _leg(now + timedelta(seconds=1), "CBTC", "USDCx", "0.001", "99.5"),
    ]
    assert WebClient.daily_loss(trades) == Decimal("0.5")


def test_daily_loss_sell_first_skips_leading_then_counts():
    """Day starts with a leftover sell (buy was yesterday) -> skipped; the
    following complete buy->sell pair still counts."""
    from datetime import timedelta
    now = datetime.now(timezone.utc).replace(hour=7)
    trades = [
        _leg(now, "CBTC", "USDCx", "0.002", "200"),               # leading sell: skip
        _leg(now + timedelta(seconds=1), "USDCx", "CBTC", "100", "0.001"),  # buy opens
        _leg(now + timedelta(seconds=2), "CBTC", "USDCx", "0.001", "98"),   # sell closes
    ]
    assert WebClient.daily_loss(trades) == Decimal("2")          # 100 - 98, leading sell ignored


def test_daily_loss_trailing_buy_not_counted():
    """Ends the day still holding the token (buy with no closing sell) -> ignored."""
    from datetime import timedelta
    now = datetime.now(timezone.utc).replace(hour=6)
    trades = [
        _leg(now, "USDCx", "CBTC", "100", "0.001"),
        _leg(now + timedelta(seconds=1), "CBTC", "USDCx", "0.001", "99"),   # cycle 1: loss 1
        _leg(now + timedelta(seconds=2), "USDCx", "CBTC", "100", "0.001"),  # trailing buy: ignore
    ]
    assert WebClient.daily_loss(trades) == Decimal("1")


def test_weekly_loss_this_week_only():
    """Weekly loss aggregates Monday-UTC..now; last week is excluded, and daily
    stays today-only. Reference pinned to a Thursday so week_start < today."""
    from datetime import timedelta
    ref = datetime(2026, 6, 15, 12, tzinfo=timezone.utc)
    ref = ref + timedelta(days=(3 - ref.weekday()) % 7)     # normalise to Thursday
    week_start = ref.date() - timedelta(days=ref.weekday())  # Monday this week
    monday = datetime.combine(week_start, ref.timetz())      # earlier this week
    prev_sun = monday - timedelta(days=1)                    # last week (Sunday)
    trades = (_cycle(monday, "CBTC", "100", "99")            # this week: loss 1
              + _cycle(ref, "CBTC", "100", "98")             # today: loss 2
              + _cycle(prev_sun, "CBTC", "100", "90"))       # last week: loss 10 (excluded)
    assert WebClient.weekly_loss(trades, now=ref) == Decimal("3")   # 1 + 2
    assert WebClient.daily_loss(trades, day=ref) == Decimal("2")    # today only


def test_count_today_and_since():
    now = datetime.now(timezone.utc)
    trades = [
        _trade(now),
        _trade(now - timedelta(hours=2)),
        _trade(now - timedelta(days=2)),   # not today, within 7d
        _trade(now - timedelta(days=10)),  # outside 7d
    ]
    # count_today counts only entries whose UTC date == today
    today = sum(1 for t in trades if t.timestamp.date() == now.date())
    assert WebClient.count_today(trades) == today
    assert WebClient.count_since(trades, 86400) >= 2      # the two recent ones
    assert WebClient.count_since(trades, 7 * 86400) == 3  # excludes the 10-day
    assert WebClient.count_since(trades, 30 * 86400) == 4


@pytest.mark.asyncio
async def test_fetch_history_via_bearer():
    provider = AsyncMock(return_value="api-key-xyz")
    wc = WebClient(token_provider=provider)
    payload = {"history_trading": [{
        "timestamp_utc": "2026-07-09 11:59:59.1+00",
        "token_input_instrument_id": "USDCX", "token_output_instrument_id": "cBTC",
        "amount_input": "3", "amount_output": "4", "pool_cid": "p",
    }]}
    wc._get_json = AsyncMock(return_value=payload)  # type: ignore[method-assign]
    trades = await wc.fetch_trading_history()
    assert len(trades) == 1 and trades[0].amount_input == Decimal("3")
    wc._get_json.assert_awaited_once_with("/v1/history/trading")


@pytest.mark.asyncio
async def test_fetch_rebates_parses_reward_activity():
    wc = WebClient(token_provider=AsyncMock(return_value="tok"))
    payload = {
        "rebates": {
            "yesterday": {"cc_amount": "0.0", "status": ""},
            "this_week": {"cc_amount": "1.5", "status": ""},
            "last_week": {"cc_amount": "0.936", "status": "Paid: 1220d...b037ba"},
        },
        "stats": {"cc_volume_24h": "812.88"},
        "wallet_address": "Cantex::abc",
    }
    wc._get_json = AsyncMock(return_value=payload)  # type: ignore[method-assign]
    reb = await wc.fetch_rebates()
    assert reb.yesterday == Decimal("0.0")
    assert reb.this_week == Decimal("1.5")
    assert reb.last_week == Decimal("0.936")
    assert reb.last_week_status.startswith("Paid")
    wc._get_json.assert_awaited_once_with("/v1/account/reward_activity")


@pytest.mark.asyncio
async def test_fetch_trading_history_parses_json():
    wc = WebClient(token_provider=AsyncMock(return_value="tok"))
    payload = {
        "history_trading": [
            {
                "timestamp_utc": "2026-07-09 11:59:59.1+00",
                "token_input_instrument_id": "USDCX",
                "token_input_instrument_admin": "a",
                "token_output_instrument_id": "cBTC",
                "token_output_instrument_admin": "b",
                "amount_input": "10.5",
                "amount_output": "0.001",
                "pool_cid": "p1",
            }
        ]
    }
    wc._get_json = AsyncMock(return_value=payload)  # type: ignore[method-assign]
    trades = await wc.fetch_trading_history()
    assert len(trades) == 1
    assert trades[0].input_id == "USDCX"
    assert trades[0].amount_input == Decimal("10.5")


# -- ccview ------------------------------------------------------------------

from cantex_bot.ccview import CCViewClient, window


@pytest.mark.asyncio
async def test_ccview_party_fee_extracts_fee_only():
    cc = CCViewClient()
    payload = {
        "data": [
            {"counterparty_id": "Cantex-validator-1::abc",
             "transfers_out_volume": "4.89", "transfers_in_volume": "0.0"},
            {"counterparty_id": "Cantex-Rewards::xyz",
             "transfers_out_volume": "0.0", "transfers_in_volume": "0.93"},
            {"counterparty_id": "someone-else::000",
             "transfers_out_volume": "5.0", "transfers_in_volume": "5.0"},
        ],
        "ans_binding": {
            "Cantex-validator-1::abc": [{"ans_name": "cantex.unverified.cns"}],
        },
    }
    cc.counterparties = AsyncMock(return_value=payload)  # type: ignore[method-assign]
    pf = await cc.party_fee("Cantex::party", "2026-07-01", "2026-07-10")
    assert pf.fee == Decimal("4.89")  # only CC out to cantex.unverified.cns


def test_ccview_window_is_inclusive_range():
    from datetime import date
    s, e = window(7)
    assert (e - s).days == 7
    assert isinstance(s, date) and isinstance(e, date)


# -- swap_all amount spec ----------------------------------------------------

from cantex_bot.swap_all import AmountError, AmountSpec


def test_amountspec_absolute():
    a = AmountSpec.parse("5")
    assert not a.is_percent and a.value == Decimal("5")
    assert a.resolve(Decimal("999")) == Decimal("5")  # ignores balance


def test_amountspec_percent():
    a = AmountSpec.parse("25%")
    assert a.is_percent and a.value == Decimal("25")
    assert a.resolve(Decimal("200")) == Decimal("50")
    assert str(a) == "25%"


def test_amountspec_full_percent_of_balance():
    assert AmountSpec.parse("100%").resolve(Decimal("0.5")) == Decimal("0.5")


@pytest.mark.parametrize("bad", ["", "0", "-3", "abc", "150%", "%"])
def test_amountspec_rejects_bad(bad):
    with pytest.raises(AmountError):
        AmountSpec.parse(bad)


# -- dashboard render smoke --------------------------------------------------

@pytest.mark.asyncio
def _fake_portfolio():
    from datetime import date
    from cantex_bot.ccview import PartyFee
    from cantex_bot.portfolio import PortfolioService
    from cantex_bot.webclient import Rebates

    usdcx = SimpleNamespace(
        instrument=InstrumentId("DSO", "USDCX"), instrument_symbol="USDCX",
        unlocked_amount=Decimal("12.5"), locked_amount=Decimal("0"))
    cc = SimpleNamespace(
        instrument=InstrumentId("DSO", "CC"), instrument_symbol="CC",
        unlocked_amount=Decimal("812.88"), locked_amount=Decimal("0"))
    info = SimpleNamespace(tokens=[usdcx, cc], address="Cantex::abc")
    web = SimpleNamespace(
        fetch_trading_history=AsyncMock(return_value=[]),
        fetch_rebates=AsyncMock(return_value=Rebates(
            Decimal("0"), Decimal("1"), Decimal("0.936"),
            last_week_status="Paid: 1220d...")))
    wallet = SimpleNamespace(
        name="w1", authed=True, ensure_auth=AsyncMock(),
        sdk=SimpleNamespace(get_account_info=AsyncMock(return_value=info)), web=web)
    import asyncio as _a
    manager = SimpleNamespace(
        names=["w1"], wallets={"w1": wallet}, get=lambda n: wallet,
        sem=_a.Semaphore(10))
    store = SimpleNamespace(
        fee_stats_today=lambda n: (None, None, 0),
        latest_fee=lambda n: Decimal("0.75"))
    ccview = SimpleNamespace(party_fee=AsyncMock(
        return_value=PartyFee(Decimal("1.16"), date(2026, 7, 3), date(2026, 7, 10))))
    return PortfolioService(manager, store, ccview)


@pytest.mark.asyncio
async def test_portfolio_refresh_populates_snap():
    svc = _fake_portfolio()
    await svc.refresh_all()
    s = svc.snaps["w1"]
    assert s.status == "ok"
    assert s.usdcx == Decimal("12.5")
    assert s.cc == Decimal("812.88")                    # CC always shown
    assert s.tokens == []                               # no strategy => no pair token
    assert s.fee_today == Decimal("1.16") and s.reb_last_week == Decimal("0.936")
    assert s.fee_now == Decimal("0.75")
    t = svc.totals()
    assert t.ok == 1 and t.cc == Decimal("812.88") and t.fee_today == Decimal("1.16")


@pytest.mark.asyncio
async def test_dashboard_keeps_finished_status():
    """After a run ends, the terminal STATUS/SWAP stay on show (target reached,
    50/50) instead of reverting to the rebate note; ROUTE clears (no live leg)."""
    from cantex_bot.dashboard import Dashboard
    from cantex_bot.runstate import RunState, DONE

    svc = _fake_portfolio()
    await svc.refresh_all()
    rs = RunState()
    rs.begin(["w1"], ["CBTC"])
    rs.set("w1", done=50, target=50, plan="target reached", route="sell CBTC→USDCX")
    rs.finish("w1", status=DONE)
    rs.end()  # whole strategy ends — terminal state must survive end()
    config = SimpleNamespace(
        network=SimpleNamespace(dry_run=False, base_url="x"),
        strategy1=SimpleNamespace(daily_swap_target=50, cc_symbol="CC"))
    dash = Dashboard(svc, config, run_state=rs)
    snap = svc.snaps["w1"]
    assert dash._status_cell("w1", snap).plain == "target reached"
    assert dash._swap_cell("w1", snap).plain == "50/50"
    assert dash._route_cell("w1").plain == "—"  # no live leg once finished


def test_retry_noise_filter_drops_sdk_retry_warnings():
    """Transient SDK retry WARNINGs are dropped; real outcomes are kept."""
    import logging
    from cantex_bot.logging_setup import _RetryNoiseFilter
    flt = _RetryNoiseFilter()

    def rec(name, level, msg):
        return logging.LogRecord(name, level, __file__, 1, msg, None, None)

    assert not flt.filter(rec("cantex_sdk._sdk", logging.WARNING,
                              "API GET /v1/account/info failed (attempt 1/4): timeout"))
    assert flt.filter(rec("cantex_sdk._sdk", logging.INFO, "Swap confirmed: x"))
    assert flt.filter(rec("cantex_sdk._sdk", logging.ERROR, "gave up after 4 attempts"))
    assert flt.filter(rec("cantex_bot.portfolio", logging.WARNING, "refresh w1 failed"))


@pytest.mark.asyncio
async def test_dashboard_render_smoke():
    import io
    from rich.console import Console as RichConsole
    from cantex_bot.dashboard import Dashboard

    svc = _fake_portfolio()
    await svc.refresh_all()
    config = SimpleNamespace(
        network=SimpleNamespace(dry_run=True, base_url="https://api.cantex.io"))
    dash = Dashboard(svc, config)
    buf = io.StringIO()
    RichConsole(file=buf, width=120).print(dash.render())
    out = buf.getvalue()
    assert "CANTEX DASHBOARD" in out
    assert "PORTFOLIO" in out and "WALLETS" in out
    assert "Paid" in out and "812" in out
