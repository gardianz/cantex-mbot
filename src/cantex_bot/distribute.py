"""Distribute: send one token from ONE wallet to MANY receivers.

The mirror image of :mod:`withdraw` (many wallets -> one address). Receivers come
from either side of the same interface:

* **internal** — other wallets in ``config.toml``. Their Canton party id is not
  in the config, so each one is authenticated once to read ``AccountInfo.address``.
* **external** — a text file of party ids, one per line, optionally with a
  per-line amount. See :func:`parse_recipients` for the accepted forms.

Sent as **one ``transfer`` per recipient**, not ``batch_transfer``. The batch
endpoint looks made for this and would cost a single network fee, but the API
refuses it — measured 2026-09-21 against a live account::

    [OK]   transfer       -> target : build ok
    [OK]   transfer       -> self   : build ok
    [FAIL] batch_transfer -> 1 target : API error 401 {"error":"unauthorized address"}
    [FAIL] batch_transfer -> 2 targets: API error 401 {"error":"unauthorized address"}

A plain ``transfer`` to the very same address builds fine, and the batch fails
even with one recipient, so it is the endpoint that is closed, not the address
or the list size. If that ever changes, switching back is worth it: one fee
instead of N.

Two consequences follow from sending one at a time, and callers must handle
both: the sender pays **a network fee per recipient**, and a run can end up
**partially sent** — so every recipient carries its own result rather than the
run having a single success flag.

Transfers are irreversible. ``dry_run`` is the default and only reports.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .addresses import collect_addresses
from .markets import MarketMap
from .wallets import WalletManager
from .withdraw import WithdrawError, validate_receiver

logger = logging.getLogger(__name__)

DEFAULT_RECIPIENT_FILE = "recipients.txt"


@dataclass(frozen=True)
class Recipient:
    """One destination. ``amount`` is None when the caller supplies it."""

    address: str
    amount: Decimal | None = None
    label: str = ""          # wallet name for internal, else blank

    def resolved(self, default: Decimal) -> Decimal:
        return self.amount if self.amount is not None else default


@dataclass
class SendResult:
    """One recipient's outcome. Sends are independent, so this is per address."""

    recipient: Recipient
    amount: Decimal
    sent: bool = False
    dry_run: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.sent or (self.dry_run and self.error is None)


@dataclass
class DistributeOutcome:
    sender: str
    symbol: str
    recipients: list[Recipient] = field(default_factory=list)
    results: list[SendResult] = field(default_factory=list)
    total: Decimal = Decimal(0)      # planned
    balance: Decimal = Decimal(0)
    dry_run: bool = False
    error: str | None = None         # whole-run failure, before any send

    @property
    def ok_total(self) -> Decimal:
        """Total across recipients that succeeded — not the plan, since a run can
        be partial. In a dry run that is what *would* have gone out; the caller
        knows which it is from ``dry_run``."""
        return sum((r.amount for r in self.results if r.ok), Decimal(0))

    @property
    def failed(self) -> list[SendResult]:
        return [r for r in self.results if r.error]

    @property
    def ok(self) -> bool:
        return (self.error is None and bool(self.results)
                and all(r.ok for r in self.results))


def parse_recipients(text: str) -> list[Recipient]:
    """Parse a recipient file into addresses, with optional per-line amounts.

    Blank lines and ``#`` comments are ignored. Each line is one of::

        Cantex::1220abc…                 address only, caller's amount applies
        Cantex::1220abc…,12.5            comma-separated amount
        Cantex::1220abc… 12.5            whitespace-separated amount

    A per-line amount wins over the shared one, so a file can mix equal shares
    with a few specific payouts.
    """
    out: list[Recipient] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p for p in line.replace(",", " ").split() if p]
        address = parts[0]
        try:
            address = validate_receiver(address)
        except WithdrawError as exc:
            raise WithdrawError(f"line {lineno}: {exc}") from None
        amount: Decimal | None = None
        if len(parts) > 1:
            try:
                amount = Decimal(parts[1])
            except InvalidOperation:
                raise WithdrawError(
                    f"line {lineno}: {parts[1]!r} is not a number") from None
            if amount <= 0:
                raise WithdrawError(f"line {lineno}: amount must be > 0")
        if len(parts) > 2:
            raise WithdrawError(
                f"line {lineno}: expected 'address' or 'address amount', "
                f"got {len(parts)} fields")
        out.append(Recipient(address=address, amount=amount))

    # Two lines for one address would silently double that payout.
    counts: dict[str, int] = {}
    for r in out:
        counts[r.address] = counts.get(r.address, 0) + 1
    dupes = sorted(a for a, c in counts.items() if c > 1)
    if dupes:
        raise WithdrawError(f"duplicate addresses in file: {', '.join(dupes)}")
    if not out:
        raise WithdrawError("no recipients found (only blanks/comments)")
    return out


def load_recipients(path: str | Path = DEFAULT_RECIPIENT_FILE) -> list[Recipient]:
    p = Path(path)
    if not p.exists():
        raise WithdrawError(
            f"recipient file not found: {p}. "
            "Copy recipients.example.txt and fill in the addresses.")
    return parse_recipients(p.read_text())


async def internal_recipients(
    manager: WalletManager, names: list[str], *, exclude: str = "",
) -> list[Recipient]:
    """Resolve configured wallets to their Canton party ids.

    Shares :func:`addresses.collect_addresses` with the address export, but
    applies the opposite failure rule: a wallet that cannot be read is an error,
    not a skip. Quietly leaving one out would shrink the distribution without
    saying so.
    """
    wanted = [n for n in names if n != exclude]
    if not wanted:
        raise WithdrawError("no destination wallets selected")
    found, failed = await collect_addresses(manager, wanted)
    if failed:
        detail = "; ".join(f"{n}: {e}" for n, e in failed.items())
        raise WithdrawError(f"cannot read address for {detail}")
    return [Recipient(address=validate_receiver(a.address), label=a.name)
            for a in found]


def plan_total(recipients: list[Recipient], default: Decimal) -> Decimal:
    return sum((r.resolved(default) for r in recipients), Decimal(0))


async def distribute(
    manager: WalletManager,
    *,
    sender: str,
    symbol: str,
    recipients: list[Recipient],
    amount: Decimal,
    keep: Decimal = Decimal(0),
    dry_run: bool = True,
    memo: str = "",
    notifier=None,
    on_result: Callable[[SendResult, int, int], None] | None = None,
    cooldown: float = 1.0,
) -> DistributeOutcome:
    """Send ``amount`` of ``symbol`` from ``sender`` to every recipient.

    ``amount`` is the per-recipient default; a recipient carrying its own amount
    overrides it. ``keep`` is a floor the sender's balance must not fall below.

    One ``transfer`` per recipient (see the module docstring for why not
    ``batch_transfer``), so **each costs its own network fee** and a run can be
    partially sent: one failure does not stop the rest, and every recipient's
    outcome is reported separately. The full planned total is still checked
    against the balance up front, so a run that cannot finish does not start.

    ``on_result(result, index, total)`` fires as each send completes, so a caller
    can report progress live instead of waiting for the whole list.
    """
    out = DistributeOutcome(sender=sender, symbol=symbol.upper(),
                            recipients=list(recipients), dry_run=dry_run)
    if not recipients:
        out.error = "no recipients"
        return out
    if keep < 0:
        out.error = "keep must be >= 0"
        return out

    total = plan_total(recipients, amount)
    out.total = total
    try:
        wallet = manager.get(sender)
        await wallet.ensure_auth()
        market = await MarketMap.build(wallet.sdk)
        instrument = market.instrument(out.symbol)
        info = await wallet.sdk.get_account_info()
        out.balance = info.get_balance(instrument)
    except Exception as exc:  # noqa: BLE001
        out.error = str(exc)
        logger.error("[%s] distribute setup failed: %s", sender, exc)
        return out

    spendable = out.balance - keep
    if total > spendable:
        out.error = (
            f"need {total} {out.symbol} but only {spendable} is spendable "
            f"(balance {out.balance}, keep {keep})"
        )
        logger.warning("[%s] distribute refused: %s", sender, out.error)
        return out

    count = len(recipients)
    for i, recipient in enumerate(recipients, 1):
        each = recipient.resolved(amount)
        result = SendResult(recipient=recipient, amount=each, dry_run=dry_run)
        who = recipient.label or recipient.address[:20]
        if dry_run:
            logger.info("[%s] DRY-RUN send %s %s -> %s",
                        sender, each, out.symbol, who)
        else:
            try:
                await wallet.sdk.transfer(each, instrument, recipient.address, memo)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
                result.error = str(exc)
                logger.error("[%s] send %s %s -> %s failed: %s",
                             sender, each, out.symbol, who, exc)
            else:
                result.sent = True
                logger.info("[%s] sent %s %s -> %s",
                            sender, each, out.symbol, who)
        out.results.append(result)
        if on_result is not None:
            on_result(result, i, count)
        if result.sent and i < count:
            await asyncio.sleep(cooldown)   # only pace real submissions

    # Reporting must never lose the result. By here the transfers are final, so
    # anything that goes wrong building or sending this line is logged and
    # swallowed — raising would hide from the caller what already happened.
    if notifier is not None:
        try:
            bad = len(out.failed)
            tag = "🧪 DRY-RUN " if dry_run else "📤 "
            await notifier.send(
                f"{tag}distribute {out.ok_total} {out.symbol} from {sender} to "
                f"{count - bad}/{count} recipient(s)"
                + (f", {bad} failed" if bad else "")
            )
        except Exception as exc:  # noqa: BLE001 - the sends already happened
            logger.error("[%s] distribute notification failed: %s", sender, exc)
    return out
