"""Distribute: send one token from ONE wallet to MANY receivers.

The mirror image of :mod:`withdraw` (many wallets -> one address). Receivers come
from either side of the same interface:

* **internal** — other wallets in ``config.toml``. Their Canton party id is not
  in the config, so each one is authenticated once to read ``AccountInfo.address``.
* **external** — a text file of party ids, one per line, optionally with a
  per-line amount. See :func:`parse_recipients` for the accepted forms.

Sent with the SDK's ``batch_transfer``: **one ledger transaction for the whole
list**, so the sender pays one network fee rather than one per receiver. That is
also why a single bad address fails the entire batch — the ledger either accepts
the transaction or it does not. Addresses are therefore validated before
anything is signed, and the CLI shows the full list and total first.

Transfers are irreversible. ``dry_run`` is the default and only reports.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

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
class DistributeOutcome:
    sender: str
    symbol: str
    recipients: list[Recipient] = field(default_factory=list)
    total: Decimal = Decimal(0)
    balance: Decimal = Decimal(0)
    sent: bool = False
    dry_run: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.sent or (self.dry_run and self.error is None)


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

    The address is not in ``config.toml`` — it comes from ``AccountInfo`` — so
    each wallet authenticates once here. A wallet that cannot be read is an
    error, not a skip: leaving it out would silently shrink the distribution.
    """
    out: list[Recipient] = []
    for name in names:
        if name == exclude:
            continue
        wallet = manager.get(name)
        try:
            await wallet.ensure_auth()
            info = await wallet.sdk.get_account_info()
        except Exception as exc:  # noqa: BLE001 - report which wallet, then stop
            raise WithdrawError(f"cannot read address for {name}: {exc}") from None
        out.append(Recipient(address=validate_receiver(info.address), label=name))
    if not out:
        raise WithdrawError("no destination wallets selected")
    return out


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
) -> DistributeOutcome:
    """Send ``amount`` of ``symbol`` from ``sender`` to every recipient.

    ``amount`` is the per-recipient default; a recipient carrying its own amount
    overrides it. ``keep`` is held back in the sender — worth leaving some CC,
    since every later swap pays its network fee in CC.

    One ``batch_transfer`` for the whole list, so this is all-or-nothing: the
    balance is checked against the full total before anything is signed.
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

    transfers = [
        {"receiver": r.address, "amount": r.resolved(amount)} for r in recipients
    ]
    if dry_run:
        logger.info("[%s] DRY-RUN distribute %s %s to %d recipient(s)",
                    sender, total, out.symbol, len(recipients))
    else:
        try:
            await wallet.sdk.batch_transfer(transfers, instrument, memo)
        except Exception as exc:  # noqa: BLE001 - the whole batch fails together
            out.error = str(exc)
            logger.error("[%s] distribute failed: %s", sender, exc)
            return out
        out.sent = True
        logger.info("[%s] distributed %s %s to %d recipient(s)",
                    sender, total, out.symbol, len(recipients))

    if notifier is not None:
        tag = "🧪 DRY-RUN " if dry_run else "📤 "
        await notifier.send(
            f"{tag}distribute {total} {out.symbol} from {sender} "
            f"to {len(recipients)} recipient(s)")
    return out
