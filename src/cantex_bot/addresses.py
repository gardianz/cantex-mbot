"""Read each wallet's Canton party id.

The party id is not in ``config.toml`` — it comes from ``AccountInfo.address``,
so every wallet has to authenticate once to produce it. It is public (ccview
indexes by it), but it does map a wallet name to an on-chain identity, so the
exported file is gitignored.

Two callers want this with opposite failure rules, which is why collection and
policy are separate: exporting should hand back what it could read and name what
it could not, while a distribution must fail loudly — quietly dropping a wallet
there would silently shrink the payout.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .wallets import WalletManager

logger = logging.getLogger(__name__)

DEFAULT_ADDRESS_FILE = "wallet_addresses.txt"


@dataclass(frozen=True)
class WalletAddress:
    name: str
    address: str


async def collect_addresses(
    manager: WalletManager,
    names: list[str] | None = None,
    *,
    on_result=None,
) -> tuple[list[WalletAddress], dict[str, str]]:
    """Read party ids. Returns ``(addresses, failures)`` keyed by wallet name.

    Never raises for one bad wallet — the caller decides whether a gap matters.
    ``on_result(name, address_or_None, index, total)`` fires per wallet, since
    authenticating dozens of them takes long enough to look stuck.
    """
    wanted = list(names if names is not None else manager.names)
    found: list[WalletAddress] = []
    failed: dict[str, str] = {}
    for i, name in enumerate(wanted, 1):
        address = None
        try:
            wallet = manager.get(name)
            await wallet.ensure_auth()
            info = await wallet.sdk.get_account_info()
            address = (info.address or "").strip()
            if not address:
                raise ValueError("account info has no address")
        except Exception as exc:  # noqa: BLE001 - per-wallet isolation
            failed[name] = str(exc)
            logger.warning("address for %s failed: %s", name, exc)
        else:
            found.append(WalletAddress(name=name, address=address))
        if on_result is not None:
            on_result(name, address, i, len(wanted))
    return found, failed


def format_addresses(entries: list[WalletAddress]) -> str:
    """One party id per line, each preceded by a ``# name`` comment.

    The comment lines keep the file readable and make it valid input for
    ``distribute`` — :func:`distribute.parse_recipients` ignores ``#`` lines — so
    an export can be used as a recipient list unchanged. For a bare list of ids,
    ``grep -v '^#'``.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# cantex-bot wallet party ids — {len(entries)} wallet(s), {stamp}",
        "# Usable as a recipients file: '#' lines are ignored by the parser.",
        "",
    ]
    for e in entries:
        lines.append(f"# {e.name}")
        lines.append(e.address)
    return "\n".join(lines) + "\n"


def write_addresses(
    path: str | Path, entries: list[WalletAddress],
) -> int:
    """Write the export, returning how many addresses were written."""
    Path(path).write_text(format_addresses(entries))
    return len(entries)
