"""Bulk withdraw: send a token from many wallets to one receiver.

Uses the SDK's operator-key ``transfer`` (no intent/trading key needed). Amounts
are an absolute value or a percent of balance, optionally leaving a ``keep``
reserve behind — typically some CC, since every later swap pays its network fee
in CC and a wallet drained to zero can no longer trade.

Transfers are irreversible, so the CLI arms this explicitly and ``dry_run``
(the default) only reports what would be sent.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal

from . import runstate as run_status
from .markets import MarketMap
from .swap_all import AmountSpec
from .wallets import WalletManager

logger = logging.getLogger(__name__)


class WithdrawError(Exception):
    """Bad receiver or unusable withdraw request."""


@dataclass
class WithdrawOutcome:
    wallet: str
    symbol: str
    receiver: str
    amount: Decimal = Decimal(0)
    balance: Decimal = Decimal(0)
    sent: bool = False          # a real transfer was submitted and accepted
    dry_run: bool = False
    skipped: str | None = None  # why nothing was sent (zero amount, …)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.sent or (self.dry_run and not self.error and not self.skipped)


def validate_receiver(receiver: str) -> str:
    """Basic sanity check on a Canton party id (``Prefix::hash``). Rejects an
    empty or clearly malformed address before any funds move."""
    r = (receiver or "").strip()
    if not r:
        raise WithdrawError("receiver address is empty")
    if "::" not in r or len(r) < 8:
        raise WithdrawError(
            f"receiver {r!r} does not look like a Canton party id (Prefix::hash)")
    return r


async def withdraw_selected(
    manager: WalletManager,
    *,
    wallet_names: list[str],
    symbol: str,
    receiver: str,
    amount: AmountSpec,
    keep: Decimal = Decimal(0),
    dry_run: bool = True,
    memo: str = "",
    run_state=None,
    notifier=None,
    cooldown: float = 1.0,
) -> list[WithdrawOutcome]:
    """Send ``amount`` of ``symbol`` from each wallet to ``receiver``.

    The balance is re-read per wallet, ``keep`` is subtracted from it first, and
    a percent applies to what is left, so a percentage can never dip into the
    reserve. Wallets with nothing to send are skipped, and one wallet's failure
    never stops the rest.
    """
    receiver = validate_receiver(receiver)
    if keep < 0:
        raise WithdrawError("keep must be >= 0")
    sym = symbol.upper()

    def _st(name: str, **kw) -> None:
        if run_state is not None:
            run_state.set(name, **kw)

    if run_state is not None:
        for name in wallet_names:
            v = run_state.view(name)
            v.active, v.finished = True, False
            v.status, v.route, v.plan = run_status.RUNNING, "", "queued"
            v.done, v.target = 0, 1

    outcomes: list[WithdrawOutcome] = []
    for name in wallet_names:
        out = WithdrawOutcome(wallet=name, symbol=sym, receiver=receiver,
                              dry_run=dry_run)
        route = f"withdraw {sym}→{receiver[:12]}…"
        try:
            wallet = manager.get(name)
            await wallet.ensure_auth()
            market = await MarketMap.build(wallet.sdk)
            instrument = market.instrument(sym)
            info = await wallet.sdk.get_account_info()
            out.balance = info.get_balance(instrument)
            spendable = out.balance - keep
            out.amount = amount.resolve(spendable) if spendable > 0 else Decimal(0)
            if out.amount <= 0:
                out.skipped = f"nothing to send (balance {out.balance}, keep {keep})"
                _st(name, status=run_status.STOPPED, route=route, plan="saldo kurang")
                logger.info("[%s] withdraw skipped: %s", name, out.skipped)
                outcomes.append(out)
                continue
            _st(name, status=run_status.SWAPPING, route=route,
                plan=f"{'dry' if dry_run else 'kirim'} {out.amount:.4f}")
            if dry_run:
                logger.info("[%s] DRY-RUN withdraw %s %s -> %s",
                            name, out.amount, sym, receiver[:20])
            else:
                await wallet.sdk.transfer(out.amount, instrument, receiver, memo)
                out.sent = True
                logger.info("[%s] withdrew %s %s -> %s",
                            name, out.amount, sym, receiver[:20])
            _st(name, status=run_status.RUNNING, route=route,
                plan="withdraw ok", done=1)
        except Exception as exc:  # noqa: BLE001 - one wallet must not stop the rest
            out.error = str(exc)
            _st(name, status=run_status.ERROR, route=route, plan="withdraw gagal")
            logger.error("[%s] withdraw failed: %s", name, exc)
        if run_state is not None:
            run_state.finish(
                name, status=run_status.DONE if out.ok else run_status.STOPPED)
        outcomes.append(out)
        await asyncio.sleep(cooldown)

    if notifier is not None:
        ok = sum(1 for o in outcomes if o.ok)
        total = sum((o.amount for o in outcomes if o.ok), Decimal(0))
        tag = "🧪 DRY-RUN " if dry_run else "📤 "
        await notifier.send(
            f"{tag}withdraw {sym}: {ok}/{len(outcomes)} wallets, "
            f"{total} {sym} → {receiver[:20]}…")
    return outcomes
