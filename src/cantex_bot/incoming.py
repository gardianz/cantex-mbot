"""Pending incoming transfers: accept tokens a wallet has not pre-approved.

A token without a ``TransferPreapproval`` (see :mod:`preapproval`) is not
credited when someone sends it. The transfer waits as a pending Canton
``TransferInstruction`` that the receiver must accept, and it lapses at
``execute_before`` — 24h after it was sent, measured 2026-09-24 — after which
only the sender can take it back. Forty wallets funded by one distribute run
all sat on the same 20 USDC.B this way, unspendable and on a clock.

Both halves come from the API directly, as with pre-approvals:

  * status — ``GET /v1/account/info`` → ``tokens[].pending_deposit_transfers[]``
    carrying ``contract_id``, ``amount``, ``sender`` and ``execute_before``.
    The SDK's ``TokenBalance`` keeps only the contract ids, so the raw
    response is read.
  * accept — ``POST /v1/ledger/transaction/build/transfer_action`` with
    ``{"transferInstructionCid": cid, "choice": "accept"}``, then the SDK's
    operator-key build → sign → submit. It is the endpoint the SDK already
    uses with ``"withdraw"`` to reclaim; a build-only probe (never signed or
    submitted) got back ``invalid choice. Must be one of: accept, reject,
    update, withdraw`` for a made-up choice and a built transaction for
    ``accept``.

Accepting is a ledger transaction and pays a network fee **per transfer**, so
the CLI arms it the way withdraws are armed. Accepting clears what is pending
now; only a pre-approval stops the next transfer of that token from waiting.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from . import runstate as run_status
from .logging_setup import wallet_logs
from .wallets import WalletManager
from .webclient import WebClientError, _parse_ts

logger = logging.getLogger(__name__)

INFO_PATH = "/v1/account/info"
ACTION_PATH = "/v1/ledger/transaction/build/transfer_action"


def _ts(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        return _parse_ts(str(raw))
    except WebClientError:
        return None


@dataclass(frozen=True)
class PendingTransfer:
    """One transfer waiting for this wallet to accept it."""

    symbol: str
    contract_id: str
    amount: Decimal
    sender: str
    execute_before: datetime | None
    requested_at: datetime | None

    @classmethod
    def _from_raw(cls, symbol: str, raw: dict) -> PendingTransfer:
        return cls(
            symbol=symbol,
            contract_id=raw["contract_id"],
            amount=Decimal(str(raw.get("amount") or "0")),
            sender=raw.get("sender") or "",
            execute_before=_ts(raw.get("execute_before")),
            requested_at=_ts(raw.get("requested_at")),
        )

    def expired(self, now: datetime | None = None) -> bool:
        """Past ``execute_before``: it can no longer be accepted, only
        withdrawn by the sender. Unknown deadline = still worth trying."""
        if self.execute_before is None:
            return False
        return (now or datetime.now(timezone.utc)) >= self.execute_before


def totals(transfers: list[PendingTransfer]) -> dict[str, Decimal]:
    """Amount per token symbol."""
    out: dict[str, Decimal] = {}
    for t in transfers:
        out[t.symbol] = out.get(t.symbol, Decimal(0)) + t.amount
    return out


def earliest_deadline(transfers: list[PendingTransfer]) -> datetime | None:
    deadlines = [t.execute_before for t in transfers if t.execute_before]
    return min(deadlines) if deadlines else None


async def fetch_pending(wallet) -> list[PendingTransfer]:
    """Every transfer waiting on this wallet, oldest deadline first."""
    raw = await wallet.sdk._request("GET", INFO_PATH)
    out: list[PendingTransfer] = []
    for tok in raw.get("tokens", []) or []:
        symbol = tok.get("instrument_symbol") or tok.get("instrument_id") or "?"
        for p in tok.get("pending_deposit_transfers", []) or []:
            if p.get("contract_id"):
                out.append(PendingTransfer._from_raw(symbol, p))
    far = datetime.max.replace(tzinfo=timezone.utc)
    out.sort(key=lambda t: t.execute_before or far)
    return out


@dataclass
class AcceptOutcome:
    wallet: str
    dry_run: bool = False
    accepted: list[PendingTransfer] = field(default_factory=list)
    expired: list[PendingTransfer] = field(default_factory=list)
    failed: list[tuple[PendingTransfer, str]] = field(default_factory=list)
    error: str | None = None                  # whole-wallet failure

    @property
    def ok(self) -> bool:
        return self.error is None and not self.failed


async def accept_wallet(
    wallet,
    *,
    dry_run: bool = True,
    only_symbols: list[str] | None = None,
    cooldown: float = 1.0,
) -> AcceptOutcome:
    """Accept every pending incoming transfer on one wallet.

    Re-reads the list rather than trusting a survey taken minutes earlier: a
    transfer may have been accepted from the web meanwhile, or lapsed. One
    transfer's failure never stops the others.
    """
    out = AcceptOutcome(wallet=wallet.name, dry_run=dry_run)
    only = {s.upper() for s in only_symbols} if only_symbols else None
    try:
        await wallet.ensure_auth()
        pending = await fetch_pending(wallet)
    except Exception as exc:  # noqa: BLE001 - one wallet must not stop the rest
        out.error = str(exc)
        logger.error("[%s] pending transfers unreadable: %s", wallet.name, exc)
        return out

    for t in pending:
        if only is not None and t.symbol.upper() not in only:
            continue
        if t.expired():
            out.expired.append(t)
            logger.warning("[%s] %s %s lapsed at %s — only the sender can "
                           "withdraw it now", wallet.name, t.amount, t.symbol,
                           t.execute_before)
            continue
        if dry_run:
            out.accepted.append(t)
            logger.info("[%s] DRY-RUN accept %s %s from %s", wallet.name,
                        t.amount, t.symbol, t.sender[:24])
            continue
        try:
            await wallet.sdk._build_sign_submit(
                ACTION_PATH,
                {"transferInstructionCid": t.contract_id, "choice": "accept"},
            )
        except Exception as exc:  # noqa: BLE001 - per-transfer isolation
            out.failed.append((t, str(exc)))
            logger.error("[%s] accept %s %s failed: %s",
                         wallet.name, t.amount, t.symbol, exc)
            continue
        out.accepted.append(t)
        logger.info("[%s] accepted %s %s from %s", wallet.name, t.amount,
                    t.symbol, t.sender[:24])
        await asyncio.sleep(cooldown)   # only pace real submissions
    return out


async def accept_selected(
    manager: WalletManager,
    *,
    wallet_names: list[str],
    dry_run: bool = True,
    only_symbols: list[str] | None = None,
    run_state=None,
    notifier=None,
    on_result: Callable[[AcceptOutcome, int, int], None] | None = None,
    cooldown: float = 1.0,
) -> list[AcceptOutcome]:
    """Run :func:`accept_wallet` over each wallet in turn.

    ``on_result(outcome, index, total)`` fires as each wallet finishes, so a
    long batch reports live instead of looking frozen (logging is file-only).
    """
    def _st(name: str, **kw) -> None:
        if run_state is not None:
            run_state.set(name, **kw)

    if run_state is not None:
        for name in wallet_names:
            v = run_state.view(name)
            v.active, v.finished = True, False
            v.status, v.route, v.plan = run_status.RUNNING, "", "queued"
            v.done, v.target = 0, 1

    outcomes: list[AcceptOutcome] = []
    total = len(wallet_names)
    route = "terima transfer"
    for i, name in enumerate(wallet_names, 1):
        wallet = manager.get(name)
        _st(name, status=run_status.SWAPPING, route=route, plan="proses terima")
        with wallet_logs(name):
            out = await accept_wallet(
                wallet, dry_run=dry_run, only_symbols=only_symbols,
                cooldown=cooldown,
            )
        if out.error or out.failed:
            _st(name, status=run_status.ERROR, route=route, plan="terima gagal")
        else:
            _st(name, status=run_status.RUNNING, route=route,
                plan=f"diterima {len(out.accepted)}", done=1)
        if run_state is not None:
            run_state.finish(
                name, status=run_status.DONE if out.ok else run_status.STOPPED)
        outcomes.append(out)
        if on_result is not None:
            on_result(out, i, total)

    # Already-submitted ledger transactions must not be hidden by a bad summary.
    if notifier is not None:
        try:
            got = totals([t for o in outcomes for t in o.accepted])
            n_ok = sum(len(o.accepted) for o in outcomes)
            n_bad = sum(len(o.failed) for o in outcomes) + sum(
                1 for o in outcomes if o.error)
            n_lapsed = sum(len(o.expired) for o in outcomes)
            tag = "🧪 DRY-RUN " if dry_run else "✅ "
            amounts = ", ".join(f"{v.normalize():f} {k}" for k, v in got.items())
            await notifier.send(
                f"{tag}incoming transfers: {n_ok} accepted"
                + (f" ({amounts})" if amounts else "")
                + f" across {len(outcomes)} wallet(s)"
                + (f", {n_bad} failed" if n_bad else "")
                + (f", {n_lapsed} lapsed" if n_lapsed else "")
            )
        except Exception as exc:  # noqa: BLE001 - the accepts already happened
            logger.error("incoming-transfer notification failed: %s", exc)
    return outcomes
