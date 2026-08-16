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


@pytest.mark.asyncio
async def test_strategy_fee_rejection_never_aborts_wallet(tmp_path, monkeypatch):
    """A 'maxNetworkFee reached' rejection is a market condition, not a failure.
    The fee oscillates around the limit all day, so counting these toward the
    abort cap killed every funded wallet with 'stopped: repeated errors'. The
    wallet must keep polling indefinitely instead."""
    import asyncio as _a
    strat, engine, rs, store = _strategy_fixture(tmp_path, monkeypatch, retries=99)
    engine.dry_run = False
    stop = _a.Event()
    calls = {"n": 0}

    async def _fee_rejected(*_a_, **_kw):
        calls["n"] += 1
        if calls["n"] >= 12:      # twice the abort cap of max(6, 1 pair * 2) = 6
            stop.set()
        return SimpleNamespace(
            counted=False, ok=False, error=None, submitted_attempt=False,
            fee_rejected=True, guard=None, buy_amount=Decimal("0"),
            buy_symbol="CBTC",
            reject_reasons=["network fee rose above 0.7 between quote and submit"],
        )

    engine.execute_swap = AsyncMock(side_effect=_fee_rejected)
    strat.config = Strategy1Config(daily_swap_target=99, insufficient_retries=99,
                                   cooldown_seconds=0, poll_max_seconds=0)
    strat._token_cc_value = AsyncMock(return_value=Decimal("0"))          # flat -> buy
    strat._balances = AsyncMock(return_value=(Decimal("0"), Decimal("100"), Decimal("50")))
    await strat._run_wallet(SimpleNamespace(name="w1", ensure_auth=AsyncMock(),
                                            sdk=SimpleNamespace()), stop)
    assert calls["n"] == 12                       # kept going well past the cap
    assert rs.view("w1").plan == "fee naik >0.7"  # waiting, not stopped
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


def test_parse_dump_label_styles_and_name_norm():
    from cantex_bot.wallet_import import parse_dump
    m = "word " * 24
    dump = "\n".join([
        "grass_1", m, "Cantex::1220aa", "e0" * 32, "98" * 32,        # bare hex
        "", "ani_1", m, "Cantex::1220bb", "op: " + "37" * 32, "tk: " + "6f" * 32,
        "", "cantex danu_one", m, "Cantex::1220cc",
        "operator key: " + "d7" * 32, "trading key: " + "6b" * 32,
    ])
    ws = parse_dump(dump)
    assert [w.name for w in ws] == ["grass_1", "ani_1", "danu_one"]   # "cantex " stripped
    assert ws[0].operator == "e0" * 32 and ws[0].trading == "98" * 32
    assert ws[1].operator == "37" * 32 and ws[2].operator == "d7" * 32


def test_parse_dump_rejects_misaligned():
    from cantex_bot.wallet_import import parse_dump, WalletImportError
    with pytest.raises(WalletImportError):
        parse_dump("name\nword word word\nCantex::x\ndeadbeef")   # 4 lines


def test_append_wallets_idempotent(tmp_path, monkeypatch):
    from cantex_bot import wallet_import as wi
    monkeypatch.setattr(wi, "assert_gitignored", lambda p: None)   # tmp files, skip git check
    env = tmp_path / ".env"
    cfg = tmp_path / "config.toml"
    env.write_text("CANTEX_W1_OPERATOR_KEY=aa\n")
    cfg.write_text('[[wallets]]\nname = "w1"\n')
    ws = wi.parse_dump("\n".join(["grass_1", "word " * 24, "Cantex::z", "e0" * 32, "98" * 32]))
    env_add, cfg_add = wi.append_wallets(ws, env_path=str(env), config_path=str(cfg))
    assert len(env_add) == 1 and len(cfg_add) == 1
    assert "CANTEX_GRASS_1_OPERATOR_KEY=" + "e0" * 32 in env.read_text()
    assert 'name = "grass_1"' in cfg.read_text()
    # second run adds nothing (idempotent)
    assert wi.append_wallets(ws, env_path=str(env), config_path=str(cfg)) == ([], [])


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


@pytest.mark.asyncio
async def test_engine_max_fee_rejection_is_not_an_error(tmp_path):
    """The API answers 'maxNetworkFee reached' when the network fee rises between
    the quote and the submit. It refuses the intent BEFORE executing it, so this
    must read as a rejection to wait out — error stays None and submitted_attempt
    is cleared, or Strategy1 would reconcile a swap that cannot exist and count a
    market condition toward its abort cap."""
    from cantex_sdk import CantexError
    store = Store(tmp_path / "s.db")
    sdk = SimpleNamespace(
        get_swap_quote=AsyncMock(return_value=make_quote(net="0.55")),
        swap_and_confirm=AsyncMock(side_effect=CantexError(
            'API error 400: {"error":"maxNetworkFee reached"}')),
    )
    wallet = fake_wallet(sdk)
    engine = SwapEngine(SwapGuard(GuardConfig(max_network_fee=Decimal("0.56"))),
                        store, notifier(), dry_run=False)
    out = await engine.execute_swap(
        wallet, sell=InstrumentId("DSO", "USDCX"), buy=InstrumentId("DSO", "CETH"),
        sell_amount=Decimal("13"), sell_symbol="USDCX", buy_symbol="CETH",
        direction="buy", quiet_reject=True,
    )
    assert out.fee_rejected and out.reject_reasons
    assert out.error is None                  # a wait, not a failure
    assert not out.submitted_attempt          # nothing settled, nothing to confirm
    assert not out.counted and not out.executed
    store.close()


@pytest.mark.asyncio
async def test_engine_other_swap_errors_stay_errors(tmp_path):
    """Only the max-fee rejection is reclassified — any other CantexError is still
    a failure with submitted_attempt set, so the ambiguous-swap reconciliation
    still runs for a genuine confirm timeout."""
    from cantex_sdk import CantexError
    store = Store(tmp_path / "s.db")
    sdk = SimpleNamespace(
        get_swap_quote=AsyncMock(return_value=make_quote(net="0.1")),
        swap_and_confirm=AsyncMock(side_effect=CantexError("confirm timeout")),
    )
    wallet = fake_wallet(sdk)
    engine = SwapEngine(SwapGuard(GuardConfig()), store, notifier(), dry_run=False)
    out = await engine.execute_swap(
        wallet, sell=InstrumentId("DSO", "USDCX"), buy=InstrumentId("DSO", "CETH"),
        sell_amount=Decimal("13"), sell_symbol="USDCX", buy_symbol="CETH",
        direction="buy", quiet_reject=True,
    )
    assert out.error and out.submitted_attempt and not out.fee_rejected
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
    # A short first page ends the paging after one request.
    wc._get_json.assert_awaited_once_with("/v1/history/trading?limit=200&offset=0")


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


# -- transfer pre-approvals --------------------------------------------------

def _preapproval_wallet(*, active: set[str] = frozenset(), submit=None):
    """A wallet whose raw /v1/account/admin lists three tokens, some pre-approved."""
    def tok(sym: str) -> dict:
        contracts = ({"transfer_preapproval": {"contract_id": f"cid::{sym}"}}
                     if sym in active else
                     {"transfer_preapproval": {"contract_id": None}})
        return {"instrument_id": sym, "instrument_admin": "DSO",
                "instrument_symbol": sym, "contracts": contracts}

    raw = {"tokens": [tok("CC"), tok("CBTC"), tok("HECTO")]}
    sdk = SimpleNamespace(
        _request=AsyncMock(return_value=raw),
        _build_sign_submit=submit or AsyncMock(return_value={"id": "x"}),
    )
    return SimpleNamespace(name="w1", ensure_auth=AsyncMock(), sdk=sdk)


@pytest.mark.asyncio
async def test_fetch_token_approvals_reads_raw_contracts():
    """AccountAdmin drops the per-token contracts block, so status must come from
    the raw response: a transfer_preapproval contract_id means ACTIVE."""
    from cantex_bot.preapproval import ADMIN_PATH, fetch_token_approvals
    wallet = _preapproval_wallet(active={"CC"})
    toks = await fetch_token_approvals(wallet)
    assert {t.symbol: t.active for t in toks} == {
        "CC": True, "CBTC": False, "HECTO": False}
    wallet.sdk._request.assert_awaited_once_with("GET", ADMIN_PATH)


@pytest.mark.asyncio
async def test_approve_wallet_skips_already_active():
    """Re-creating a live pre-approval would pay a second network fee for nothing."""
    from cantex_bot.preapproval import BUILD_PATH, approve_wallet
    submit = AsyncMock(return_value={"id": "x"})
    wallet = _preapproval_wallet(active={"CC"}, submit=submit)
    out = await approve_wallet(wallet, dry_run=False, cooldown=0)
    assert out.already == ["CC"]
    assert sorted(out.approved) == ["CBTC", "HECTO"] and out.ok
    assert submit.await_count == 2
    paths = {c.args[0] for c in submit.await_args_list}
    payloads = {c.args[1]["instrumentId"] for c in submit.await_args_list}
    assert paths == {BUILD_PATH} and payloads == {"CBTC", "HECTO"}
    assert submit.await_args_list[0].args[1]["instrumentAdmin"] == "DSO"


@pytest.mark.asyncio
async def test_approve_wallet_dry_run_submits_nothing():
    from cantex_bot.preapproval import approve_wallet
    submit = AsyncMock()
    wallet = _preapproval_wallet(submit=submit)
    out = await approve_wallet(wallet, dry_run=True, cooldown=0)
    assert sorted(out.approved) == ["CBTC", "CC", "HECTO"]
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_wallet_one_bad_token_does_not_stop_the_rest():
    from cantex_bot.preapproval import approve_wallet
    calls = {"n": 0}

    async def _submit(_path, payload):
        calls["n"] += 1
        if payload["instrumentId"] == "CBTC":
            raise RuntimeError("insufficient CC for fee")
        return {"id": "x"}

    wallet = _preapproval_wallet(submit=AsyncMock(side_effect=_submit))
    out = await approve_wallet(wallet, dry_run=False, cooldown=0)
    assert calls["n"] == 3                       # all three attempted
    assert out.failed == {"CBTC": "insufficient CC for fee"}
    assert sorted(out.approved) == ["CC", "HECTO"] and not out.ok


@pytest.mark.asyncio
async def test_approve_wallet_reports_status_failure_per_wallet():
    """A wallet whose admin read fails is reported, not raised — one bad wallet
    must never abort a batch."""
    from cantex_bot.preapproval import approve_wallet
    wallet = _preapproval_wallet()
    wallet.sdk._request = AsyncMock(side_effect=RuntimeError("timed out"))
    out = await approve_wallet(wallet, dry_run=False, cooldown=0)
    assert out.error == "timed out" and not out.ok and out.approved == []


# -- connection pooling ------------------------------------------------------

@pytest.mark.asyncio
async def test_shared_connector_is_reused_across_clients():
    """Every Cantex client must borrow ONE pool, or a warm connection opened for
    one wallet cannot serve the next and each sweep pays a cold connect."""
    from cantex_bot import nethelp
    await nethelp.close_shared_connector()
    try:
        a = nethelp.shared_connector()
        b = nethelp.shared_connector()
        assert a is b
    finally:
        await nethelp.close_shared_connector()


@pytest.mark.asyncio
async def test_shared_connector_survives_a_borrowing_session_closing():
    """Sessions pass connector_owner=False, so closing one must not take the
    shared pool (and every other wallet's connections) down with it."""
    import aiohttp
    from cantex_bot import nethelp
    await nethelp.close_shared_connector()
    try:
        pool = nethelp.shared_connector()
        session = aiohttp.ClientSession(connector=pool, connector_owner=False)
        await session.close()
        assert not pool.closed
        assert nethelp.shared_connector() is pool
    finally:
        await nethelp.close_shared_connector()


@pytest.mark.asyncio
async def test_shared_connector_rebuilt_after_close():
    from cantex_bot import nethelp
    await nethelp.close_shared_connector()
    first = nethelp.shared_connector()
    await nethelp.close_shared_connector()
    assert first.closed
    second = nethelp.shared_connector()
    try:
        assert second is not first and not second.closed
    finally:
        await nethelp.close_shared_connector()


def test_keepalive_outlives_a_refresh_sweep():
    """Regression: keepalive_timeout == refresh_interval expired every pooled
    connection just as the next sweep needed it, so a stable host still logged
    'GET /v1/account/info timed out after 4 attempts'."""
    from cantex_bot.config import PerformanceConfig
    from cantex_bot.nethelp import _KEEPALIVE
    assert _KEEPALIVE > PerformanceConfig.refresh_interval


# -- proxy support -----------------------------------------------------------

@pytest.fixture
def _no_proxy(monkeypatch):
    """Isolate the module-level proxy + the env vars configure_proxy exports."""
    from cantex_bot import nethelp
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setattr(nethelp, "_proxy", None)
    yield nethelp
    monkeypatch.setattr(nethelp, "_proxy", None)


def test_configure_proxy_exports_http_env(_no_proxy):
    """An http proxy must reach the SDK, which builds its own requests and never
    passes proxy= — the env var plus trust_env=True is the only route in."""
    import os
    _no_proxy.configure_proxy("http://10.0.0.1:8080")
    assert os.environ["HTTPS_PROXY"] == "http://10.0.0.1:8080"
    assert os.environ["HTTP_PROXY"] == "http://10.0.0.1:8080"
    assert _no_proxy.proxy_url() == "http://10.0.0.1:8080"


def test_configure_proxy_empty_is_direct(_no_proxy):
    import os
    _no_proxy.configure_proxy("   ")
    assert _no_proxy.proxy_url() is None
    assert "HTTPS_PROXY" not in os.environ


def test_configure_proxy_rejects_unknown_scheme(_no_proxy):
    from cantex_bot.nethelp import ProxyError
    with pytest.raises(ProxyError, match="expected http"):
        _no_proxy.configure_proxy("ftp://10.0.0.1:21")
    with pytest.raises(ProxyError):
        _no_proxy.configure_proxy("10.0.0.1:8080")      # no scheme
    assert _no_proxy.proxy_url() is None                # nothing half-applied


def test_socks_proxy_reports_missing_extra(monkeypatch, _no_proxy):
    """socks5 without aiohttp-socks must fail loudly at startup, not silently
    fall back to a direct connection that the VPS cannot make."""
    import builtins
    from cantex_bot.nethelp import ProxyError
    real_import = builtins.__import__

    def _no_socks(name, *a, **kw):
        if name == "aiohttp_socks":
            raise ImportError("no module")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_socks)
    with pytest.raises(ProxyError, match="socks"):
        _no_proxy.configure_proxy("socks5://127.0.0.1:40000")


def test_socks_proxy_does_not_export_http_env(monkeypatch, _no_proxy):
    """SOCKS is applied at the connector; exporting HTTPS_PROXY as well would
    make aiohttp try to CONNECT to a SOCKS port over HTTP."""
    import os
    import sys
    monkeypatch.setitem(sys.modules, "aiohttp_socks", SimpleNamespace(
        ProxyConnector=SimpleNamespace(from_url=lambda url, **kw: None)))
    _no_proxy.configure_proxy("socks5://127.0.0.1:40000")
    assert _no_proxy.proxy_url() == "socks5://127.0.0.1:40000"
    assert "HTTPS_PROXY" not in os.environ


def test_make_connector_tunnels_through_proxy(monkeypatch, _no_proxy):
    import sys
    seen = {}

    def _from_url(url, **kw):
        seen["url"], seen["kw"] = url, kw
        return "socks-connector"

    monkeypatch.setitem(sys.modules, "aiohttp_socks", SimpleNamespace(
        ProxyConnector=SimpleNamespace(from_url=_from_url)))
    got = _no_proxy.make_connector(limit=7, proxy="socks5://127.0.0.1:40000")
    assert got == "socks-connector"
    assert seen["url"] == "socks5://127.0.0.1:40000"
    assert seen["kw"]["limit"] == 7
    assert seen["kw"]["keepalive_timeout"] == _no_proxy._KEEPALIVE


@pytest.mark.asyncio
async def test_single_socks_proxy_applies_without_a_proxy_map(monkeypatch, _no_proxy):
    """A lone socks5:// proxy has no env-var route in (that is http-only), so the
    default pool must pick it up — otherwise it would silently go direct."""
    import sys
    seen = []
    monkeypatch.setitem(sys.modules, "aiohttp_socks", SimpleNamespace(
        ProxyConnector=SimpleNamespace(
            from_url=lambda url, **kw: seen.append(url) or SimpleNamespace(closed=False))))
    from cantex_bot import nethelp
    nethelp._pools.clear()
    try:
        nethelp.configure_proxy("socks5://127.0.0.1:40000")
        nethelp.connector_for(None)
        assert seen == ["socks5://127.0.0.1:40000"]
    finally:
        nethelp._pools.clear()


@pytest.mark.parametrize("line,expected", [
    ("http://u:p@h:8080", "http://u:p@h:8080"),
    ("socks5://127.0.0.1:40000", "socks5://127.0.0.1:40000"),
    ("203.0.113.12:8080", "http://203.0.113.12:8080"),
    ("203.0.113.13:8080:me:secret", "http://me:secret@203.0.113.13:8080"),
    ("me:secret@203.0.113.14:8080", "http://me:secret@203.0.113.14:8080"),
])
def test_proxy_line_formats(line, expected):
    """Proxy providers export several shapes; all normalise to one URL."""
    from cantex_bot.proxies import normalise
    assert normalise(line) == expected


@pytest.mark.parametrize("bad", ["h:8080:only:three:many", "ftp://h:21", "", "justhost"])
def test_proxy_line_rejected(bad):
    from cantex_bot.proxies import ProxyFileError, normalise
    with pytest.raises(ProxyFileError):
        normalise(bad)


def test_parse_proxies_skips_blanks_and_comments():
    from cantex_bot.proxies import parse_proxies
    text = "# header\n\nh1:1\n   \n# note\nsocks5://h2:2\n"
    assert parse_proxies(text) == ["http://h1:1", "socks5://h2:2"]


def test_assign_is_index_based_and_wraps():
    """Index-based, not round-robin: a wallet must keep the same egress IP
    across restarts, and fewer proxies than wallets is a normal setup."""
    from cantex_bot.proxies import assign
    m = assign(["w1", "w2", "w3", "w4", "w5"], ["pA", "pB"])
    assert m == {"w1": "pA", "w2": "pB", "w3": "pA", "w4": "pB", "w5": "pA"}
    assert assign(["w1"], []) == {}


def test_load_proxies_rejects_missing_and_empty(tmp_path):
    """Silently running direct would defeat the point of configuring proxies."""
    from cantex_bot.proxies import ProxyFileError, load_proxies
    with pytest.raises(ProxyFileError, match="not found"):
        load_proxies(tmp_path / "nope.txt")
    empty = tmp_path / "p.txt"
    empty.write_text("# only a comment\n\n")
    with pytest.raises(ProxyFileError, match="no proxy entries"):
        load_proxies(empty)


def test_redact_hides_credentials():
    from cantex_bot.proxies import redact
    assert redact("http://me:secret@h:8080") == "http://***@h:8080"
    assert redact("socks5://h:1080") == "socks5://h:1080"


@pytest.mark.asyncio
async def test_connector_pooled_per_proxy(monkeypatch, _no_proxy):
    """One pool per egress, not per wallet: wallets sharing a proxy must share
    warm connections, but must not share a pool with a different egress."""
    import sys
    made = []
    monkeypatch.setitem(sys.modules, "aiohttp_socks", SimpleNamespace(
        ProxyConnector=SimpleNamespace(
            from_url=lambda url, **kw: made.append(url) or SimpleNamespace(closed=False))))
    from cantex_bot import nethelp
    await nethelp.close_shared_connector()
    try:
        a1 = nethelp.connector_for("socks5://a:1")
        a2 = nethelp.connector_for("socks5://a:1")
        b = nethelp.connector_for("socks5://b:2")
        direct = nethelp.connector_for(None)
        assert a1 is a2 and a1 is not b
        assert direct is nethelp.shared_connector()   # None == the direct pool
        assert made == ["socks5://a:1", "socks5://b:2"]
    finally:
        nethelp._pools.clear()
        await nethelp.close_shared_connector()


def test_config_proxy_file_env_override(tmp_path, monkeypatch):
    from cantex_bot.config import load_config
    cfg = tmp_path / "config.toml"
    cfg.write_text('[network]\nproxy_file = "a.txt"\n\n[[wallets]]\nname = "w1"\n')
    monkeypatch.setenv("CANTEX_W1_OPERATOR_KEY", "aa" * 32)
    assert load_config(cfg, env_path=None).network.proxy_file == "a.txt"
    monkeypatch.setenv("CANTEX_PROXY_FILE", "b.txt")
    assert load_config(cfg, env_path=None).network.proxy_file == "b.txt"


def test_config_proxy_env_overrides_file(tmp_path, monkeypatch):
    """A VPS sets CANTEX_PROXY without editing a config shared with other hosts."""
    from cantex_bot.config import load_config
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[network]\nproxy = "http://from-file:1"\n\n[[wallets]]\nname = "w1"\n')
    monkeypatch.setenv("CANTEX_W1_OPERATOR_KEY", "aa" * 32)
    monkeypatch.setenv("CANTEX_PROXY", "socks5://from-env:2")
    assert load_config(cfg, env_path=None).network.proxy == "socks5://from-env:2"
    monkeypatch.delenv("CANTEX_PROXY")
    assert load_config(cfg, env_path=None).network.proxy == "http://from-file:1"


# -- per-wallet log view -----------------------------------------------------

def _ring_logger(monkeypatch):
    """A logger wired to a fresh ring buffer, as setup() would wire it."""
    import logging as _l
    from cantex_bot import logging_setup as ls
    from collections import deque
    monkeypatch.setattr(ls, "_RING", deque(maxlen=200))
    handler = ls._RingHandler()
    handler.setFormatter(_l.Formatter("%(levelname)s %(name)s %(message)s"))
    log = _l.getLogger("cantex_bot.test_ring")
    log.handlers = [handler]
    log.setLevel(_l.INFO)
    log.propagate = False
    return log, ls


def test_recent_logs_filters_by_wallet_tag(monkeypatch):
    """The [name] prefix every per-wallet log line already uses is what attributes
    a record, so one wallet's log can be read without the others interleaved."""
    log, ls = _ring_logger(monkeypatch)
    log.info("[w1] buy 10 USDCX->CBTC")
    log.info("[w2] sell 3 CBTC->USDCX")
    log.info("[w1] swap berhasil")
    assert ls.recent_logs(10, wallet="w1") == [
        "INFO cantex_bot.test_ring [w1] buy 10 USDCX->CBTC",
        "INFO cantex_bot.test_ring [w1] swap berhasil",
    ]
    assert len(ls.recent_logs(10)) == 3          # unfiltered still sees everything


def test_wallet_logs_context_tags_sdk_records(monkeypatch):
    """Records from below the per-wallet loop — the SDK's especially — carry no
    wallet of their own. Without the context they would be invisible in a
    per-wallet view, which is exactly the line needed when a wallet stalls."""
    log, ls = _ring_logger(monkeypatch)
    with ls.wallet_logs("grass_3"):
        log.warning("API GET /v1/account/info returned 502 (attempt 1/4)")
    log.warning("API GET /v1/account/info returned 502 (attempt 1/4)")  # outside
    assert ls.recent_logs(10, wallet="grass_3") == [
        "WARNING cantex_bot.test_ring API GET /v1/account/info returned 502 (attempt 1/4)",
    ]


def test_wallet_logs_context_restores_previous(monkeypatch):
    """Nested/sequential blocks must not leak — a wallet's context ends with it."""
    log, ls = _ring_logger(monkeypatch)
    with ls.wallet_logs("w1"):
        log.info("inner")
    log.info("outer")
    assert ls.recent_logs(10, wallet="w1") == ["INFO cantex_bot.test_ring inner"]


def test_explicit_tag_beats_context(monkeypatch):
    """A parent task logging about a specific wallet wins over its own context."""
    log, ls = _ring_logger(monkeypatch)
    with ls.wallet_logs("w1"):
        log.error("[w2] crashed")
    assert ls.recent_logs(10, wallet="w2") == ["ERROR cantex_bot.test_ring [w2] crashed"]
    assert ls.recent_logs(10, wallet="w1") == []


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


def test_retry_noise_filter_can_keep_attempts_for_diagnosis():
    """CANTEX_LOG_RETRIES=1 keeps the attempt lines: the dropped WARNING is the
    only place the real exception behind 'timed out after 4 attempts' appears."""
    import logging
    from cantex_bot.logging_setup import _RetryNoiseFilter
    rec = logging.LogRecord(
        "cantex_sdk._sdk", logging.WARNING, __file__, 1,
        "API GET /v1/account/info failed (attempt 1/4): "
        "Connection timeout to host, retrying in 1.0s", None, None)
    assert not _RetryNoiseFilter().filter(rec)
    assert _RetryNoiseFilter(keep_retries=True).filter(rec)


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("YES", True), ("on", True),
     (None, False), ("", False), ("0", False), ("false", False), ("off", False)],
)
def test_truthy_env_parsing(value, expected):
    from cantex_bot.logging_setup import _truthy
    assert _truthy(value) is expected


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
    RichConsole(file=buf, width=200).print(dash.render())
    out = buf.getvalue()
    assert "CANTEX DASHBOARD" in out
    assert "PORTFOLIO" in out and "WALLETS" in out
    assert "Paid" in out and "812" in out
    assert "PROFIT" in out and "LOSS" in out


@pytest.mark.asyncio
async def test_dashboard_log_panel_follows_cursor(monkeypatch):
    """The LOG panel shows the cursor wallet's records only, so a stalled wallet
    can be read on its own; 'l' toggles back to the unfiltered stream."""
    import io
    from rich.console import Console as RichConsole
    from cantex_bot.dashboard import Dashboard
    from cantex_bot.portfolio import WalletSnap

    log, _ls = _ring_logger(monkeypatch)
    log.info("[w1] buy 10 USDCX->CBTC")
    log.info("[w2] stuck waiting fee")

    svc = _fake_portfolio()
    svc.manager = SimpleNamespace(names=["w1", "w2"])
    svc.snaps = {"w1": WalletSnap(name="w1"), "w2": WalletSnap(name="w2")}
    config = SimpleNamespace(
        network=SimpleNamespace(dry_run=True, base_url="https://api.cantex.io"))
    dash = Dashboard(svc, config)

    def panel() -> str:
        buf = io.StringIO()
        RichConsole(file=buf, width=200).print(dash._log_panel())
        return buf.getvalue()

    dash.cursor = 0
    out = panel()
    assert "LOG " in out and "w1" in out
    assert "buy 10 USDCX" in out and "stuck waiting fee" not in out

    dash.cursor = 1                                   # cursor moves -> log follows
    out = panel()
    assert "stuck waiting fee" in out and "buy 10 USDCX" not in out

    assert dash._on_key(b"l", 10, 2) is False          # 'l' = show every wallet
    out = panel()
    assert "buy 10 USDCX" in out and "stuck waiting fee" in out


# -- Telegram command bot (/stats) -----------------------------------------

def test_chunk_lines_splits_on_line_boundaries():
    from cantex_bot.telegram import _chunk_lines
    text = "\n".join(f"line{i}" for i in range(10))  # 10 * 6 chars + newlines
    chunks = _chunk_lines(text, 20)
    assert all(len(c) <= 20 for c in chunks)
    # No line is lost or split across chunks (each line stays whole).
    assert "\n".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_chunk_lines_hard_splits_overlong_line():
    from cantex_bot.telegram import _chunk_lines
    chunks = _chunk_lines("x" * 45, 20)
    assert [len(c) for c in chunks] == [20, 20, 5]


def _command_bot(monkeypatch):
    from cantex_bot.telegram import TelegramCommandBot, TelegramNotifier
    cfg = TelegramConfig(enabled=True, bot_token="tok", chat_id="123")
    notifier = TelegramNotifier(cfg)
    sent: list[str] = []
    monkeypatch.setattr(notifier, "send",
                        AsyncMock(side_effect=lambda t: sent.append(t)))
    seen: list[str] = []

    async def stats(arg):
        seen.append(arg)
        return "REPORT BODY"

    bot = TelegramCommandBot(cfg, notifier, handlers={"stats": stats})
    return bot, sent, seen


@pytest.mark.asyncio
async def test_command_bot_dispatches_stats(monkeypatch):
    bot, sent, seen = _command_bot(monkeypatch)
    await bot._dispatch({"message": {"chat": {"id": 123}, "text": "/stats"}})
    assert seen == [""]
    assert sent and sent[0].startswith("<pre>") and "REPORT BODY" in sent[0]


@pytest.mark.asyncio
async def test_command_bot_ignores_foreign_chat(monkeypatch):
    bot, sent, seen = _command_bot(monkeypatch)
    await bot._dispatch({"message": {"chat": {"id": 999}, "text": "/stats"}})
    assert seen == [] and sent == []


@pytest.mark.asyncio
async def test_command_bot_strips_botname_and_args(monkeypatch):
    bot, sent, seen = _command_bot(monkeypatch)
    await bot._dispatch({"message": {"chat": {"id": 123}, "text": "/stats@MyBot foo"}})
    assert seen == ["foo"]


@pytest.mark.asyncio
async def test_command_bot_unknown_command(monkeypatch):
    bot, sent, seen = _command_bot(monkeypatch)
    await bot._dispatch({"message": {"chat": {"id": 123}, "text": "/nope"}})
    assert seen == []
    assert sent and "Unknown command" in sent[0] and "/stats" in sent[0]


# -- history pagination + loss-in-CC + profit --------------------------------

def _hrow(i: int) -> dict:
    """A distinct trading-history row (unique pool_cid + amount for dedup).

    The timestamp is 'now' so the row always sits inside fetch_trading_history's
    cover_days window (a fixed date would fall out of it as time passes and stop
    the paging early)."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00")
    return {
        "timestamp_utc": stamp,
        "token_input_instrument_id": "USDCX", "token_output_instrument_id": "cBTC",
        "amount_input": str(i), "amount_output": "1", "pool_cid": f"p{i}",
    }


@pytest.mark.asyncio
async def test_fetch_history_pages_until_short():
    from cantex_bot.webclient import WebClient
    wc = WebClient(token_provider=AsyncMock(return_value="t"))
    page1 = {"history_trading": [_hrow(i) for i in range(200)]}
    page2 = {"history_trading": [_hrow(i) for i in range(200, 205)]}
    wc._get_json = AsyncMock(side_effect=[page1, page2])  # type: ignore[method-assign]
    trades = await wc.fetch_trading_history()
    assert len(trades) == 205
    assert wc._get_json.await_count == 2  # full page -> page 2; short page -> stop
    wc._get_json.assert_any_await("/v1/history/trading?limit=200&offset=200")


@pytest.mark.asyncio
async def test_fetch_history_stops_when_paging_ignored():
    from cantex_bot.webclient import WebClient
    wc = WebClient(token_provider=AsyncMock(return_value="t"))
    page = {"history_trading": [_hrow(i) for i in range(200)]}
    wc._get_json = AsyncMock(return_value=page)  # same page every call
    trades = await wc.fetch_trading_history()
    assert len(trades) == 200            # dupes dropped
    assert wc._get_json.await_count == 2  # page 2 adds nothing -> stop


def test_to_cc_converts_and_guards_zero():
    from cantex_bot.portfolio import PortfolioService
    assert PortfolioService._to_cc(Decimal("10"), Decimal("2")) == Decimal("5")
    assert PortfolioService._to_cc(Decimal("10"), Decimal("0")) == Decimal("0")


@pytest.mark.asyncio
async def test_profit_totals_sum_per_wallet():
    svc = _fake_portfolio()
    await svc.refresh_all()
    s = svc.snaps["w1"]
    s.profit_yesterday = Decimal("3")
    s.profit_week = Decimal("4")
    s.loss_yesterday = Decimal("1")
    t = svc.totals()
    assert t.profit_yesterday == Decimal("3")
    assert t.profit_week == Decimal("4")
    assert t.loss_yesterday == Decimal("1")


@pytest.mark.asyncio
async def test_profit_cell_colors_gain_green_loss_red():
    from cantex_bot.dashboard import Dashboard
    svc = _fake_portfolio()
    config = SimpleNamespace(
        network=SimpleNamespace(dry_run=True, base_url="x"),
        strategy1=SimpleNamespace(daily_swap_target=50, cc_symbol="CC"))
    dash = Dashboard(svc, config)
    assert dash._profit_part(Decimal("2"))[1] == "green3"
    assert dash._profit_part(Decimal("-2"))[1] == "red"
    assert dash._profit_part(Decimal("0"))[0] == "0.00"


@pytest.mark.asyncio
async def test_fee_ttl_throttles_ccview(monkeypatch):
    """Fees are fetched once, then reused until fee_ttl — no ccview call the
    second sweep (the fix for the HTTP 429 spam)."""
    svc = _fake_portfolio()
    await svc.refresh_all()
    calls_after_first = svc.ccview.party_fee.await_count
    await svc.refresh_all()  # immediate second sweep, within fee_ttl
    assert svc.ccview.party_fee.await_count == calls_after_first  # no new ccview calls


# -- swap_selected token -> token --------------------------------------------

class _FakeMarket:
    def __init__(self):
        self._m = {s: InstrumentId("D", s) for s in ("USDCX", "CBTC", "CETH")}

    def instrument(self, sym):
        return self._m[sym.upper()]


class _FakeEngine:
    def __init__(self):
        self.calls = []

    async def execute_swap(self, wallet, *, sell, buy, sell_amount,
                           sell_symbol, buy_symbol, direction, **_):
        self.calls.append((sell_symbol, buy_symbol, sell_amount, direction))
        return SimpleNamespace(ok=True, error=None, counted=True,
                               reject_reasons=[], guard=None)


def _swap_fakes():
    info = SimpleNamespace(get_balance=lambda inst: Decimal("100"))
    wallet = SimpleNamespace(
        name="w1", ensure_auth=AsyncMock(),
        sdk=SimpleNamespace(get_account_info=AsyncMock(return_value=info)))
    manager = SimpleNamespace(get=lambda n: wallet)
    return manager, _FakeEngine()


@pytest.mark.asyncio
async def test_swap_selected_token_to_token(monkeypatch):
    from cantex_bot import swap_all
    from cantex_bot.swap_all import AmountSpec, swap_selected
    monkeypatch.setattr(swap_all.MarketMap, "build",
                        AsyncMock(return_value=_FakeMarket()))
    manager, eng = _swap_fakes()
    await swap_selected(
        manager, eng, wallet_names=["w1"], token_symbols=["CETH", "CBTC"],
        usdcx_symbol="USDCX", direction="swap", amount=AmountSpec.parse("100%"),
        sell_symbol="CBTC", cooldown=0)
    # CBTC->CBTC is skipped; only CBTC->CETH fires, 100% of the 100 CBTC balance.
    assert len(eng.calls) == 1
    assert eng.calls[0] == ("CBTC", "CETH", Decimal("100"), "swap")


@pytest.mark.asyncio
async def test_swap_selected_swap_requires_sell_symbol():
    from cantex_bot.swap_all import AmountSpec, swap_selected
    with pytest.raises(ValueError):
        await swap_selected(
            SimpleNamespace(), SimpleNamespace(), wallet_names=["w1"],
            token_symbols=["CETH"], usdcx_symbol="USDCX", direction="swap",
            amount=AmountSpec.parse("5"))


# -- Strategy1 base token (token -> token) -----------------------------------

def test_strategy1_base_symbol_defaults_and_overrides():
    from cantex_bot.strategies.strategy1 import Strategy1
    from cantex_bot.config import Strategy1Config
    mgr = SimpleNamespace(wallets={}, names=[])
    cfg = Strategy1Config()
    s_default = Strategy1(mgr, SimpleNamespace(), cfg, notifier(), None)
    assert s_default.base_symbol == cfg.usdcx_symbol.upper()
    s_base = Strategy1(mgr, SimpleNamespace(), cfg, notifier(), None,
                       base_symbol="cbtc")
    assert s_base.base_symbol == "CBTC"

    class _RecMarket:
        def __init__(self):
            self.args = None

        def trade_pairs(self, base, *, only_symbols=None, exclude_symbols=()):
            self.args = (base, only_symbols, exclude_symbols)
            return []

    m = _RecMarket()
    s_base.tokens = ["CETH"]
    s_base._pairs_for(m)
    assert m.args[0] == "CBTC" and m.args[1] == ["CETH"]  # base threaded through


# -- base-aware loss / CC price ----------------------------------------------

def test_active_base_follows_runstate():
    from cantex_bot.runstate import RunState
    svc = _fake_portfolio()
    assert svc._active_base() == "USDCX"                 # no strategy -> USDCX
    rs = RunState()
    rs.begin(["w1"], ["CETH"], base_symbol="cbtc")
    svc.run_state = rs
    assert svc._active_base() == "CBTC"                  # follows the run's base


@pytest.mark.asyncio
async def test_cc_price_keyed_by_base():
    svc = _fake_portfolio()

    class _M:
        def instrument(self, s):
            return InstrumentId("D", s.upper())

    svc._market = _M()  # skip MarketMap.build
    wallet = svc.manager.get("w1")

    wallet.sdk.get_swap_quote = AsyncMock(
        return_value=SimpleNamespace(returned_amount=Decimal("20")))
    assert await svc._ensure_cc_price(wallet, "USDCX") == Decimal("2")  # 20/10
    assert svc._cc_price_base == "USDCX"

    # A base change forces a refetch (not the cached USDCX price).
    wallet.sdk.get_swap_quote = AsyncMock(
        return_value=SimpleNamespace(returned_amount=Decimal("50")))
    assert await svc._ensure_cc_price(wallet, "CBTC") == Decimal("5")   # 50/10
    assert svc._cc_price_base == "CBTC"

    # base == CC is 1:1 with no quote.
    assert await svc._ensure_cc_price(wallet, "CC") == Decimal("1")


# -- guard bypass (manual 1x swap override) ----------------------------------

@pytest.mark.asyncio
async def test_execute_swap_bypass_guards_overrides_reject(tmp_path):
    store = Store(tmp_path / "s.db")
    engine = SwapEngine(SwapGuard(GuardConfig(max_slippage=Decimal("0.1"))),
                        store, notifier(), dry_run=True)
    sdk = SimpleNamespace(get_swap_quote=AsyncMock(return_value=make_quote(slippage="5")))
    wallet = fake_wallet(sdk)
    a, b = InstrumentId("d", "USDCX"), InstrumentId("d", "CBTC")

    # Guards on: the high-slippage quote is rejected, nothing executes.
    out = await engine.execute_swap(
        wallet, sell=a, buy=b, sell_amount=Decimal("5"),
        sell_symbol="USDCX", buy_symbol="CBTC", direction="buy")
    assert out.reject_reasons and not out.ok

    # Bypass: the same quote executes (dry-run here), counted, no reject.
    out2 = await engine.execute_swap(
        wallet, sell=a, buy=b, sell_amount=Decimal("5"),
        sell_symbol="USDCX", buy_symbol="CBTC", direction="buy", bypass_guards=True)
    assert out2.ok and out2.counted and not out2.reject_reasons
    store.close()


# -- per-pair fees + base column ---------------------------------------------

def test_pair_fee_stats(tmp_path):
    store = Store(tmp_path / "s.db")
    store.record_fee("w1", "A->B", Decimal("0.70"), Decimal("0.05"), Decimal("0.10"))
    store.record_fee("w2", "A->B", Decimal("0.72"), Decimal("0.06"), Decimal("0.11"))
    store.record_fee("w1", "C->D", Decimal("0.90"))
    stats = {p: (lat, mn, av, slip, pool, n)
             for p, lat, mn, av, slip, pool, n in store.pair_fee_stats()}
    assert set(stats) == {"A->B", "C->D"}
    lat, mn, av, slip, pool, n = stats["A->B"]
    assert n == 2 and mn == Decimal("0.7") and lat == Decimal("0.72")  # latest row
    assert slip == Decimal("0.06") and pool == Decimal("0.11")         # latest slip/pool
    assert stats["C->D"][5] == 1                                       # count
    store.close()


def test_dashboard_base_column_follows_runstate():
    from cantex_bot.dashboard import Dashboard
    from cantex_bot.runstate import RunState
    svc = _fake_portfolio()
    cfg = SimpleNamespace(
        network=SimpleNamespace(dry_run=True, base_url="x"),
        strategy1=SimpleNamespace(daily_swap_target=50, cc_symbol="CC",
                                  usdcx_symbol="USDCX"))
    assert Dashboard(svc, cfg)._base_symbol() == "USDCX"          # default
    rs = RunState()
    rs.begin(["w1"], ["USDCX"], base_symbol="frxusd.b")
    assert Dashboard(svc, cfg, rs)._base_symbol() == "FRXUSD.B"   # follows run


# -- Strategy 2 (auto lowest-fee) --------------------------------------------

def _s2(store):
    from cantex_bot.strategies.strategy2 import Strategy2
    from cantex_bot.config import Strategy1Config
    engine = SimpleNamespace(guard=SwapGuard(GuardConfig()))
    return Strategy2(SimpleNamespace(wallets={}, names=[]), engine,
                     Strategy1Config(), notifier(), store, tokens=["CBTC", "CETH"])


def _s2_pairs():
    from cantex_bot.markets import Pair
    usdcx = InstrumentId("a", "USDCX")
    cbtc = InstrumentId("a", "CBTC")
    ceth = InstrumentId("a", "CETH")
    pairs = [Pair(cbtc, "CBTC", usdcx, ""), Pair(ceth, "CETH", usdcx, "")]
    return usdcx, InstrumentId("a", "CC"), cbtc, ceth, pairs


@pytest.mark.asyncio
async def test_strategy2_pick_sells_held_first():
    usdcx, cc, cbtc, ceth, pairs = _s2_pairs()
    bals = {usdcx: Decimal("100"), cbtc: Decimal("0.5"), ceth: Decimal("0"), cc: Decimal("50")}
    info = SimpleNamespace(get_balance=lambda i: bals[i])
    wallet = SimpleNamespace(name="w1",
                             sdk=SimpleNamespace(get_account_info=AsyncMock(return_value=info)))
    strat = _s2(None)
    strat._token_cc_value = AsyncMock(return_value=Decimal("110"))  # >= min ticket
    strat._lowest_fee_pair = AsyncMock()                            # must NOT run
    pair, sellable, tbal, ubal, cbal = await strat._pick(
        wallet, pairs, usdcx, cc, {"notional": Decimal("10")})
    assert sellable and pair.token_symbol == "CBTC" and tbal == Decimal("0.5")
    strat._lowest_fee_pair.assert_not_awaited()


@pytest.mark.asyncio
async def test_strategy2_pick_buys_lowest_fee_when_flat():
    usdcx, cc, cbtc, ceth, pairs = _s2_pairs()
    bals = {usdcx: Decimal("100"), cbtc: Decimal("0"), ceth: Decimal("0"), cc: Decimal("50")}
    info = SimpleNamespace(get_balance=lambda i: bals[i])
    wallet = SimpleNamespace(name="w1",
                             sdk=SimpleNamespace(get_account_info=AsyncMock(return_value=info)))
    strat = _s2(None)
    strat._token_cc_value = AsyncMock(return_value=Decimal("0"))
    strat._lowest_fee_pair = AsyncMock(return_value=pairs[1])       # CETH
    pair, sellable, tbal, ubal, cbal = await strat._pick(
        wallet, pairs, usdcx, cc, {"notional": Decimal("10")})
    assert not sellable and pair.token_symbol == "CETH"
    strat._lowest_fee_pair.assert_awaited_once()


@pytest.mark.asyncio
async def test_strategy2_lowest_fee_pair_picks_min_and_records(tmp_path):
    usdcx, cc, cbtc, ceth, pairs = _s2_pairs()
    fees = {"CBTC": Decimal("0.80"), "CETH": Decimal("0.60")}

    def quote(amount, sell, buy):
        return make_quote(net=str(fees[buy.id]))

    wallet = SimpleNamespace(name="w1",
                             sdk=SimpleNamespace(get_swap_quote=AsyncMock(side_effect=quote)))
    store = Store(tmp_path / "s.db")
    strat = _s2(store)
    best = await strat._lowest_fee_pair(wallet, pairs, usdcx, Decimal("10"))
    assert best.token_symbol == "CETH"                             # lowest fee
    recorded = {p for p, *_ in store.pair_fee_stats()}
    assert recorded == {"USDCX->CBTC", "USDCX->CETH"}              # all probed fees logged
    store.close()


# -- dashboard cursor + fee probe --------------------------------------------

def test_dashboard_cursor_navigation():
    from cantex_bot.dashboard import Dashboard
    svc = SimpleNamespace(
        manager=SimpleNamespace(names=[f"w{i}" for i in range(10)]),
        store=SimpleNamespace())
    cfg = SimpleNamespace(
        network=SimpleNamespace(dry_run=True, base_url="x"),
        strategy1=SimpleNamespace(daily_swap_target=1, cc_symbol="CC", usdcx_symbol="USDCX"))
    d = Dashboard(svc, cfg)
    n, page = 10, 3
    assert d._on_key(b"\x1b[B", page, n) is False      # down
    assert d.cursor == 1 and d.offset == 0
    for _ in range(5):
        d._on_key(b"\x1b[B", page, n)                  # cursor -> 6
    assert d.cursor == 6 and d.offset == 4             # scrolled to keep visible
    d._on_key(b"g", page, n)
    assert d.cursor == 0 and d.offset == 0
    d._on_key(b"G", page, n)
    assert d.cursor == 9 and d.offset == 7
    d._on_key(b"\x1b[A", page, n)                      # up
    assert d.cursor == 8
    assert d._on_key(b"q", page, n) is True            # quit


@pytest.mark.asyncio
async def test_maybe_probe_fees_records_all_pairs(tmp_path):
    from cantex_bot.markets import Pair
    svc = _fake_portfolio()
    store = Store(tmp_path / "s.db")
    svc.store = store
    usdcx = InstrumentId("a", "USDCX")
    cbtc = InstrumentId("a", "CBTC")
    ceth = InstrumentId("a", "CETH")

    class _M:
        def instrument(self, s):
            return {"USDCX": usdcx, "CBTC": cbtc, "CETH": ceth,
                    "CC": InstrumentId("a", "CC")}[s.upper()]

        def trade_pairs(self, base, exclude_symbols=()):
            return [Pair(cbtc, "CBTC", usdcx, ""), Pair(ceth, "CETH", usdcx, "")]

    svc._market = _M()
    svc._cc_price = Decimal("1")
    svc.fee_probe_interval = 0.0            # always due
    wallet = svc.manager.get("w1")
    wallet.authed = True
    fees = {"CBTC": Decimal("0.5"), "CETH": Decimal("0.7")}

    def quote(amount, sell, buy):
        return make_quote(net=str(fees[buy.id]))

    wallet.sdk.get_swap_quote = AsyncMock(side_effect=quote)
    await svc._maybe_probe_fees()
    recorded = {p for p, *_ in store.pair_fee_stats()}
    assert recorded == {"USDCX->CBTC", "USDCX->CETH"}   # every pair probed
    store.close()


# -- loss pairs by token (base-change / multi-token fix) ---------------------

def test_loss_pairs_by_token_not_cross():
    """Interleaved cycles of different tokens must pair per token, not close a
    CBTC buy with a FRXUSD.B sell (the base-change 'kacau' bug)."""
    from cantex_bot.webclient import WebClient, Trade
    now = datetime.now(timezone.utc)

    def tr(i, inp, out, ai, ao):
        return Trade(timestamp=now.replace(microsecond=i), input_id=inp,
                     input_admin="", output_id=out, output_admin="",
                     amount_input=Decimal(ai), amount_output=Decimal(ao),
                     pool_cid=f"p{i}")

    trades = [
        tr(1, "USDCX", "CBTC", "20", "0.001"),        # buy CBTC, spent 20
        tr(2, "USDCX", "FRXUSD.B", "200", "199"),     # buy FRXUSD.B, spent 200
        tr(3, "CBTC", "USDCX", "0.001", "19"),        # sell CBTC -> 20-19 = 1
        tr(4, "FRXUSD.B", "USDCX", "199", "195"),     # sell FRXUSD.B -> 200-195 = 5
    ]
    loss = WebClient.daily_loss(trades, usdcx_symbol="USDCX", day=now)
    assert loss == Decimal("6")   # 1 + 5, NOT the cross-paired 200-19 = 181


def test_loss_fifo_same_token():
    """Two buys then two sells of the same token pair FIFO."""
    from cantex_bot.webclient import WebClient, Trade
    now = datetime.now(timezone.utc)

    def tr(i, inp, out, ai, ao):
        return Trade(timestamp=now.replace(microsecond=i), input_id=inp,
                     input_admin="", output_id=out, output_admin="",
                     amount_input=Decimal(ai), amount_output=Decimal(ao),
                     pool_cid=f"p{i}")

    trades = [
        tr(1, "USDCX", "CBTC", "10", "0.5"),
        tr(2, "USDCX", "CBTC", "20", "1.0"),
        tr(3, "CBTC", "USDCX", "0.5", "9"),    # closes first buy: 10-9 = 1
        tr(4, "CBTC", "USDCX", "1.0", "18"),   # closes second buy: 20-18 = 2
    ]
    assert WebClient.daily_loss(trades, usdcx_symbol="USDCX", day=now) == Decimal("3")


def test_pair_fee_stats_merges_case(tmp_path):
    """A pair must be one row regardless of symbol case (USDCX vs USDCx). Legacy
    mixed-case rows are normalised by the startup migration."""
    from datetime import datetime, timezone
    p = tmp_path / "s.db"
    today = datetime.now(timezone.utc).date().isoformat()
    store = Store(p)
    # Legacy raw mixed-case row, as an old DB would hold it.
    store._conn.execute(
        "INSERT INTO fee_obs (ts, day, wallet, pair, network_fee, slippage, "
        "pool_fee) VALUES (?,?,?,?,?,?,?)",
        (1.0, today, "w2", "USDCx->FRXUSD.B", 0.72, 0, 0))
    store._conn.commit()
    store.close()
    # Re-open: the migration upper-cases legacy pairs so they merge.
    store = Store(p)
    store.record_fee("w1", "USDCX->FRXUSD.B", Decimal("0.70"))
    rows = store.pair_fee_stats()
    assert [r[0] for r in rows] == ["USDCX->FRXUSD.B"]   # single merged pair
    assert rows[0][6] == 2                               # both observations
    store.close()


@pytest.mark.asyncio
async def test_swap_selected_updates_run_state(monkeypatch):
    """Swap 1x publishes per-wallet progress so the dashboard ROUTE/STATUS/SWAP
    columns move instead of sitting idle."""
    from cantex_bot import swap_all
    from cantex_bot.swap_all import AmountSpec, swap_selected
    from cantex_bot.runstate import RunState
    monkeypatch.setattr(swap_all.MarketMap, "build",
                        AsyncMock(return_value=_FakeMarket()))
    manager, eng = _swap_fakes()
    rs = RunState()
    await swap_selected(
        manager, eng, wallet_names=["w1"], token_symbols=["CBTC", "CETH"],
        usdcx_symbol="USDCX", direction="buy", amount=AmountSpec.parse("10"),
        run_state=rs, cooldown=0)
    v = rs.view("w1")
    assert v.finished and not v.active         # frozen on the dashboard after run
    assert v.done == 2 and v.target == 2       # both swaps ok
    assert v.status == "done"
    assert v.route == "buy USDCX→CETH"         # last route shown


# -- dashboard key tokeniser (smooth scrolling) ------------------------------

def test_split_keys_multiple_and_partial():
    from cantex_bot.dashboard import _split_keys
    keys, rem = _split_keys(b"\x1b[A\x1b[A\x1b[B")     # three arrows in one read
    assert keys == [b"\x1b[A", b"\x1b[A", b"\x1b[B"] and rem == b""
    keys, rem = _split_keys(b"j\x1b")                  # bare ESC at end -> held
    assert keys == [b"j"] and rem == b"\x1b"
    keys, rem = _split_keys(b"\x1b[")                  # incomplete CSI -> held
    assert keys == [] and rem == b"\x1b["
    keys, rem = _split_keys(rem + b"A")                # next read completes it
    assert keys == [b"\x1b[A"] and rem == b""
    keys, rem = _split_keys(b"gGq")                    # plain letters
    assert keys == [b"g", b"G", b"q"] and rem == b""


def test_dashboard_burst_of_arrows_moves_cursor():
    from cantex_bot.dashboard import Dashboard, _split_keys
    svc = SimpleNamespace(
        manager=SimpleNamespace(names=[f"w{i}" for i in range(10)]),
        store=SimpleNamespace())
    cfg = SimpleNamespace(
        network=SimpleNamespace(dry_run=True, base_url="x"),
        strategy1=SimpleNamespace(daily_swap_target=1, cc_symbol="CC", usdcx_symbol="USDCX"))
    d = Dashboard(svc, cfg)
    keys, _ = _split_keys(b"\x1b[B\x1b[B\x1b[B")        # 3 downs arriving together
    for k in keys:
        d._on_key(k, 5, 10)
    assert d.cursor == 3                                # all applied, not dropped


def test_notifier_pauses_then_recovers():
    """Repeated failures pause Telegram for a cooldown, not forever (a transient
    blip must not kill it for the whole session)."""
    import time as _t
    from cantex_bot.telegram import TelegramNotifier
    n = TelegramNotifier(TelegramConfig(enabled=True, bot_token="t", chat_id="1"))
    assert n.enabled
    for _ in range(5):
        n._note_failure("boom")
    assert not n.enabled                     # paused after _MAX_FAILS
    n._paused_until = _t.monotonic() - 1      # cooldown elapsed
    assert n.enabled                          # recovers, not permanently disabled


# -- loss brakes (cycle-loss guard + daily budget) ---------------------------

def _brake_strat(store, **cfg):
    from cantex_bot.strategies.strategy1 import Strategy1
    from cantex_bot.config import Strategy1Config
    engine = SimpleNamespace(guard=SwapGuard(GuardConfig()), dry_run=True)
    return Strategy1(SimpleNamespace(wallets={}, names=[]), engine,
                     Strategy1Config(**cfg), notifier(), store)


def test_last_buy_cost(tmp_path):
    store = Store(tmp_path / "s.db")
    store.record_swap(SwapRecord(wallet="w1", direction="buy", sell_symbol="USDCX",
                                 buy_symbol="CBTC", sell_amount=Decimal("10"),
                                 buy_amount=Decimal("1")))
    store.record_swap(SwapRecord(wallet="w1", direction="buy", sell_symbol="USDCX",
                                 buy_symbol="CBTC", sell_amount=Decimal("12"),
                                 buy_amount=Decimal("1")))
    assert store.last_buy_cost("w1", "usdcx", "cbtc") == Decimal("12")  # newest
    assert store.last_buy_cost("w1", "USDCX", "CETH") is None
    store.close()


@pytest.mark.asyncio
async def test_cycle_loss_pct_measures_round_trip(tmp_path):
    store = Store(tmp_path / "s.db")
    store.record_swap(SwapRecord(wallet="w1", direction="buy", sell_symbol="USDCX",
                                 buy_symbol="CBTC", sell_amount=Decimal("100"),
                                 buy_amount=Decimal("1")))
    strat = _brake_strat(store)
    base = InstrumentId("a", "USDCX"); tok = InstrumentId("a", "CBTC")
    wallet = SimpleNamespace(name="w1", sdk=SimpleNamespace(
        get_swap_quote=AsyncMock(return_value=make_quote(returned="97"))))
    pct = await strat._cycle_loss_pct(wallet, tok, "CBTC", Decimal("1"), base)
    assert pct == Decimal("3")            # spent 100, back 97 => 3% loss
    # No recorded buy -> None (unmeasurable, caller lets the sell through).
    assert await strat._cycle_loss_pct(
        wallet, tok, "CETH", Decimal("1"), base) is None
    store.close()


def test_hold_expires_after_wait(tmp_path):
    import time as _t
    store = Store(tmp_path / "s.db")
    strat = _brake_strat(store, cycle_loss_wait_seconds=60.0)
    assert not strat._hold_expired("w1", "CBTC")        # just started holding
    strat._held_since[("w1", "CBTC")] = _t.monotonic() - 61
    assert strat._hold_expired("w1", "CBTC")            # waited long enough
    strat._clear_hold("w1", "CBTC")
    assert not strat._hold_expired("w1", "CBTC")        # cleared -> restart timer
    # 0 disables the timeout entirely (hold until the price recovers).
    s2 = _brake_strat(store, cycle_loss_wait_seconds=0)
    s2._held_since[("w1", "CBTC")] = _t.monotonic() - 9999
    assert not s2._hold_expired("w1", "CBTC")
    store.close()


@pytest.mark.asyncio
async def test_daily_loss_base_cached(tmp_path):
    from cantex_bot.webclient import Trade
    store = Store(tmp_path / "s.db")
    strat = _brake_strat(store)
    now = datetime.now(timezone.utc)

    def tr(i, inp, out, ai, ao):
        return Trade(timestamp=now.replace(microsecond=i), input_id=inp,
                     input_admin="", output_id=out, output_admin="",
                     amount_input=Decimal(ai), amount_output=Decimal(ao),
                     pool_cid=f"p{i}")

    web = SimpleNamespace(fetch_trading_history=AsyncMock(return_value=[
        tr(1, "USDCX", "CBTC", "100", "1"), tr(2, "CBTC", "USDCX", "1", "94")]))
    wallet = SimpleNamespace(name="w1", web=web)
    assert await strat._daily_loss_base(wallet) == Decimal("6")
    await strat._daily_loss_base(wallet)                 # served from cache
    assert web.fetch_trading_history.await_count == 1
    store.close()


def test_daily_loss_budget_is_in_cc(tmp_path):
    """The budget uses CC (the dashboard's LOSS unit): base loss is converted
    with the same CC price the buy notional was quoted at."""
    store = Store(tmp_path / "s.db")
    strat = _brake_strat(store, cc_units=Decimal("5"))
    notional = Decimal("50")            # 5 CC is worth 50 base => 10 base per CC
    assert strat._to_cc(Decimal("30"), notional) == Decimal("3")   # 30 base = 3 CC
    assert strat._to_cc(Decimal("30"), Decimal("0")) == Decimal("0")  # unpriced
    store.close()


@pytest.mark.asyncio
async def test_cycle_brake_lets_profitable_sells_through(tmp_path):
    """A profitable round trip yields a NEGATIVE loss pct, which can never exceed
    a positive cap — the brake only ever holds losing sells."""
    store = Store(tmp_path / "s.db")
    store.record_swap(SwapRecord(wallet="w1", direction="buy", sell_symbol="USDCX",
                                 buy_symbol="CBTC", sell_amount=Decimal("100"),
                                 buy_amount=Decimal("1")))
    strat = _brake_strat(store, max_cycle_loss_pct=Decimal("0.35"))
    base = InstrumentId("a", "USDCX"); tok = InstrumentId("a", "CBTC")
    wallet = SimpleNamespace(name="w1", sdk=SimpleNamespace(
        get_swap_quote=AsyncMock(return_value=make_quote(returned="105"))))
    pct = await strat._cycle_loss_pct(wallet, tok, "CBTC", Decimal("1"), base)
    assert pct == Decimal("-5")                       # 5% gain
    assert not (pct > strat.config.max_cycle_loss_pct)  # never held
    store.close()


# -- profit override of the network-fee guard --------------------------------

def test_guard_ignore_network_fee_waives_only_that_limit():
    g = SwapGuard(GuardConfig(max_network_fee=Decimal("0.5"),
                              max_slippage=Decimal("1.0")))
    over_fee = make_quote(net="0.9")
    assert not g.evaluate(over_fee).ok                              # normally held
    assert g.evaluate(over_fee, ignore_network_fee=True).ok         # waived
    # Slippage/pool are still enforced when the fee limit is waived.
    bad_slip = make_quote(net="0.9", slippage="5")
    res = g.evaluate(bad_slip, ignore_network_fee=True)
    assert not res.ok and any("slippage" in r for r in res.reasons)


@pytest.mark.asyncio
async def test_execute_swap_ignore_network_fee_executes(tmp_path):
    store = Store(tmp_path / "s.db")
    engine = SwapEngine(SwapGuard(GuardConfig(max_network_fee=Decimal("0.5"))),
                        store, notifier(), dry_run=True)
    sdk = SimpleNamespace(get_swap_quote=AsyncMock(return_value=make_quote(net="0.9")))
    wallet = fake_wallet(sdk)
    a, b = InstrumentId("d", "CBTC"), InstrumentId("d", "USDCX")
    held = await engine.execute_swap(
        wallet, sell=a, buy=b, sell_amount=Decimal("1"),
        sell_symbol="CBTC", buy_symbol="USDCX", direction="sell")
    assert held.reject_reasons and not held.ok           # fee guard holds it
    took = await engine.execute_swap(
        wallet, sell=a, buy=b, sell_amount=Decimal("1"),
        sell_symbol="CBTC", buy_symbol="USDCX", direction="sell",
        ignore_network_fee=True)
    assert took.ok and not took.reject_reasons           # profit taken
    store.close()


# -- bulk withdraw -----------------------------------------------------------

def _wd_fakes(balance="100"):
    from cantex_bot.markets import Pair  # noqa: F401  (market shape below)
    inst = InstrumentId("d", "CC")

    class _M:
        def instrument(self, s):
            return inst

    info = SimpleNamespace(get_balance=lambda i: Decimal(balance))
    sdk = SimpleNamespace(get_account_info=AsyncMock(return_value=info),
                          transfer=AsyncMock(return_value={"ok": True}))
    wallet = SimpleNamespace(name="w1", ensure_auth=AsyncMock(), sdk=sdk)
    manager = SimpleNamespace(get=lambda n: wallet)
    return manager, wallet, _M()


def test_validate_receiver():
    from cantex_bot.withdraw import WithdrawError, validate_receiver
    assert validate_receiver("  Cantex::1220abcd  ") == "Cantex::1220abcd"
    for bad in ("", "   ", "no-colons-here", "a::b"):
        with pytest.raises(WithdrawError):
            validate_receiver(bad)


@pytest.mark.asyncio
async def test_withdraw_dry_run_sends_nothing(monkeypatch):
    from cantex_bot import withdraw as wd
    manager, wallet, market = _wd_fakes()
    monkeypatch.setattr(wd.MarketMap, "build", AsyncMock(return_value=market))
    outs = await wd.withdraw_selected(
        manager, wallet_names=["w1"], symbol="CC", receiver="Cantex::1220abcd",
        amount=AmountSpec.parse("100%"), dry_run=True, cooldown=0)
    assert outs[0].ok and outs[0].dry_run and outs[0].amount == Decimal("100")
    wallet.sdk.transfer.assert_not_awaited()          # nothing left the wallet


@pytest.mark.asyncio
async def test_withdraw_live_keeps_reserve(monkeypatch):
    from cantex_bot import withdraw as wd
    manager, wallet, market = _wd_fakes(balance="100")
    monkeypatch.setattr(wd.MarketMap, "build", AsyncMock(return_value=market))
    outs = await wd.withdraw_selected(
        manager, wallet_names=["w1"], symbol="CC", receiver="Cantex::1220abcd",
        amount=AmountSpec.parse("100%"), keep=Decimal("10"), dry_run=False,
        cooldown=0)
    assert outs[0].sent and outs[0].amount == Decimal("90")   # 100 - keep 10
    args = wallet.sdk.transfer.await_args.args
    assert args[0] == Decimal("90") and args[2] == "Cantex::1220abcd"


@pytest.mark.asyncio
async def test_withdraw_skips_when_below_keep(monkeypatch):
    from cantex_bot import withdraw as wd
    manager, wallet, market = _wd_fakes(balance="5")
    monkeypatch.setattr(wd.MarketMap, "build", AsyncMock(return_value=market))
    outs = await wd.withdraw_selected(
        manager, wallet_names=["w1"], symbol="CC", receiver="Cantex::1220abcd",
        amount=AmountSpec.parse("100%"), keep=Decimal("10"), dry_run=False,
        cooldown=0)
    assert outs[0].skipped and not outs[0].sent
    wallet.sdk.transfer.assert_not_awaited()


@pytest.mark.asyncio
async def test_withdraw_isolates_wallet_failure(monkeypatch):
    from cantex_bot import withdraw as wd
    manager, wallet, market = _wd_fakes()
    monkeypatch.setattr(wd.MarketMap, "build", AsyncMock(return_value=market))
    wallet.sdk.transfer = AsyncMock(side_effect=RuntimeError("boom"))
    outs = await wd.withdraw_selected(
        manager, wallet_names=["w1", "w1"], symbol="CC",
        receiver="Cantex::1220abcd", amount=AmountSpec.parse("1"),
        dry_run=False, cooldown=0)
    assert len(outs) == 2                       # second wallet still attempted
    assert all(o.error == "boom" and not o.ok for o in outs)


@pytest.mark.asyncio
async def test_withdraw_streams_progress_and_builds_market_once(monkeypatch):
    """Results are reported per wallet as they finish (a whole batch printed at
    the end looks frozen), and the instrument map is built once, not per wallet."""
    from cantex_bot import withdraw as wd
    manager, wallet, market = _wd_fakes()
    build = AsyncMock(return_value=market)
    monkeypatch.setattr(wd.MarketMap, "build", build)
    seen = []
    outs = await wd.withdraw_selected(
        manager, wallet_names=["w1", "w1", "w1"], symbol="CC",
        receiver="Cantex::1220abcd", amount=AmountSpec.parse("1"),
        dry_run=True, on_result=lambda o, i, n: seen.append((i, n, o.wallet)),
        cooldown=0)
    assert seen == [(1, 3, "w1"), (2, 3, "w1"), (3, 3, "w1")]  # streamed 1-by-1
    assert build.await_count == 1                              # market reused
    assert len(outs) == 3


@pytest.mark.asyncio
async def test_stop_loss_stays_armed_when_fee_guard_rejects(tmp_path, monkeypatch):
    """Once the hold times out the stop must stay armed: a fee-guard reject must
    not restart the wait (that made the stop-loss never fire), and the timer only
    restarts once the position actually closes."""
    import asyncio as _a
    import time as _t
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
    # Sell is always rejected by the fee guard (never executes).
    rejected = SimpleNamespace(counted=False, ok=False, error=None,
                               reject_reasons=["network fee 0.9 > 0.6"],
                               buy_amount=Decimal("0"), buy_symbol="USDCX",
                               submitted_attempt=False,
                               guard=SimpleNamespace(details={"network_fee": Decimal("0.9")}))
    engine = SimpleNamespace(
        dry_run=False, execute_swap=AsyncMock(return_value=rejected),
        guard=SimpleNamespace(config=SimpleNamespace(max_network_fee=Decimal("0.6"))))
    cfg = Strategy1Config(daily_swap_target=99, cooldown_seconds=0,
                          poll_min_seconds=0, poll_max_seconds=0,
                          max_cycle_loss_pct=Decimal("0.5"),
                          cycle_loss_wait_seconds=300.0,
                          insufficient_retries=99)
    strat = s1mod.Strategy1(SimpleNamespace(wallets={}, names=[]), engine, cfg,
                            notifier(), store, tokens=["CBTC"])
    strat._web_swaps_today = AsyncMock(return_value=0)
    strat._price_cc_in_usdcx = AsyncMock(return_value=Decimal("10"))
    strat._token_cc_value = AsyncMock(return_value=Decimal("110"))     # sellable
    strat._balances = AsyncMock(return_value=(Decimal("1"), Decimal("100"), Decimal("50")))
    strat._cycle_loss_pct = AsyncMock(return_value=Decimal("7"))       # 7% down
    strat._wait_next_day = AsyncMock(return_value=False)
    # Hold already timed out.
    strat._held_since[("w1", "CBTC")] = _t.monotonic() - 301

    wallet = SimpleNamespace(name="w1", ensure_auth=AsyncMock(), sdk=SimpleNamespace())
    stop = _a.Event()

    async def stop_after_two_attempts():
        while engine.execute_swap.await_count < 2:
            await _a.sleep(0)
        stop.set()

    await _a.gather(strat._run_wallet(wallet, stop), stop_after_two_attempts())

    # It kept trying (armed), and never waived the fee limit.
    assert engine.execute_swap.await_count >= 2
    assert all(c.kwargs.get("ignore_network_fee") is not True
               for c in engine.execute_swap.await_args_list)
    # Timer still expired -> the stop is still armed for the next attempt.
    assert strat._hold_expired("w1", "CBTC")
    store.close()
