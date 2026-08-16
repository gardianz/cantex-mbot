"""Transfer pre-approvals: auto-accept tokens sent to a Cantex account.

Each token needs its own Canton ``TransferPreapproval`` contract. Without one,
a token sent to the account is not credited until it is accepted by hand — the
NON-ACTIVE rows on the web's "Transfer pre-approvals" page.

The SDK exposes neither the status nor a way to create one, so both come from
the API directly (verified against the web app's own bundle):

  * status  — ``GET /v1/account/admin`` → ``tokens[].contracts.transfer_preapproval``;
    a ``contract_id`` means ACTIVE. ``AccountAdmin`` drops this field, so the raw
    response is read instead of the parsed model.
  * create  — ``POST /v1/ledger/transaction/build/transfer_preapproval`` with
    ``{instrumentAdmin, instrumentId}``, then the SDK's operator-key
    build → sign → submit flow. No intent/trading key needed.

Creating one is a real ledger transaction and pays a network fee **per token per
wallet**, so the CLI arms it the way withdraws are armed.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from cantex_sdk import InstrumentId

from . import runstate as run_status
from .logging_setup import wallet_logs
from .wallets import WalletManager

logger = logging.getLogger(__name__)

ADMIN_PATH = "/v1/account/admin"
BUILD_PATH = "/v1/ledger/transaction/build/transfer_preapproval"


@dataclass(frozen=True)
class TokenApproval:
    """One token's pre-approval state for one wallet."""

    symbol: str
    instrument: InstrumentId
    active: bool


@dataclass
class PreapprovalOutcome:
    wallet: str
    dry_run: bool = False
    already: list[str] = field(default_factory=list)   # symbols already ACTIVE
    approved: list[str] = field(default_factory=list)  # symbols activated now
    failed: dict[str, str] = field(default_factory=dict)  # symbol -> error
    error: str | None = None                           # whole-wallet failure

    @property
    def ok(self) -> bool:
        return self.error is None and not self.failed

    @property
    def pending(self) -> int:
        return len(self.approved) + len(self.failed)


async def fetch_token_approvals(wallet) -> list[TokenApproval]:
    """Every registered token for this wallet with its pre-approval state.

    Reads the raw admin response: the SDK's ``AccountAdmin`` model parses only
    the instrument identity and drops the per-token ``contracts`` block that
    carries the pre-approval.
    """
    raw = await wallet.sdk._request("GET", ADMIN_PATH)
    out: list[TokenApproval] = []
    for tok in raw.get("tokens", []) or []:
        instrument_id = tok.get("instrument_id")
        instrument_admin = tok.get("instrument_admin")
        if not instrument_id or not instrument_admin:
            continue
        contracts = tok.get("contracts") or {}
        pre = contracts.get("transfer_preapproval") or {}
        out.append(
            TokenApproval(
                symbol=tok.get("instrument_symbol") or instrument_id,
                instrument=InstrumentId(id=instrument_id, admin=instrument_admin),
                active=bool(pre.get("contract_id")),
            )
        )
    return out


async def approve_wallet(
    wallet,
    *,
    dry_run: bool = True,
    only_symbols: list[str] | None = None,
    cooldown: float = 1.0,
) -> PreapprovalOutcome:
    """Activate every pre-approval this wallet is missing.

    Tokens already ACTIVE are left alone — re-creating one would pay a second
    network fee for nothing. One token's failure never stops the others.
    """
    out = PreapprovalOutcome(wallet=wallet.name, dry_run=dry_run)
    only = {s.upper() for s in only_symbols} if only_symbols else None
    try:
        await wallet.ensure_auth()
        tokens = await fetch_token_approvals(wallet)
    except Exception as exc:  # noqa: BLE001 - one wallet must not stop the rest
        out.error = str(exc)
        logger.error("[%s] pre-approval status failed: %s", wallet.name, exc)
        return out

    for tok in tokens:
        if only is not None and tok.symbol.upper() not in only:
            continue
        if tok.active:
            out.already.append(tok.symbol)
            continue
        if dry_run:
            out.approved.append(tok.symbol)
            logger.info("[%s] DRY-RUN pre-approve %s", wallet.name, tok.symbol)
            continue
        try:
            await wallet.sdk._build_sign_submit(
                BUILD_PATH,
                {
                    "instrumentAdmin": tok.instrument.admin,
                    "instrumentId": tok.instrument.id,
                },
            )
        except Exception as exc:  # noqa: BLE001 - per-token isolation
            out.failed[tok.symbol] = str(exc)
            logger.error("[%s] pre-approve %s failed: %s",
                         wallet.name, tok.symbol, exc)
            continue
        out.approved.append(tok.symbol)
        logger.info("[%s] pre-approved %s", wallet.name, tok.symbol)
        await asyncio.sleep(cooldown)   # only pace real submissions
    return out


async def approve_selected(
    manager: WalletManager,
    *,
    wallet_names: list[str],
    dry_run: bool = True,
    only_symbols: list[str] | None = None,
    run_state=None,
    notifier=None,
    on_result: Callable[[PreapprovalOutcome, int, int], None] | None = None,
    cooldown: float = 1.0,
) -> list[PreapprovalOutcome]:
    """Run :func:`approve_wallet` over each wallet in turn.

    ``on_result(outcome, index, total)`` fires as each wallet finishes so the
    caller can report progress live — logging is file-only, so without it a long
    batch looks frozen.
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

    outcomes: list[PreapprovalOutcome] = []
    total = len(wallet_names)
    for i, name in enumerate(wallet_names, 1):
        wallet = manager.get(name)
        route = "pre-approval"
        _st(name, status=run_status.SWAPPING, route=route, plan="proses approve")
        with wallet_logs(name):
            out = await approve_wallet(
                wallet, dry_run=dry_run, only_symbols=only_symbols,
                cooldown=cooldown,
            )
        if out.error or out.failed:
            _st(name, status=run_status.ERROR, route=route, plan="approve gagal")
        else:
            _st(name, status=run_status.RUNNING, route=route,
                plan="approve ok", done=1)
        if run_state is not None:
            run_state.finish(
                name, status=run_status.DONE if out.ok else run_status.STOPPED)
        outcomes.append(out)
        if on_result is not None:
            on_result(out, i, total)

    if notifier is not None:
        done = sum(len(o.approved) for o in outcomes)
        bad = sum(len(o.failed) for o in outcomes) + sum(
            1 for o in outcomes if o.error)
        tag = "🧪 DRY-RUN " if dry_run else "✅ "
        await notifier.send(
            f"{tag}pre-approvals: {done} token(s) across {len(outcomes)} wallet(s)"
            + (f", {bad} failed" if bad else "")
        )
    return outcomes
