#!/usr/bin/env python3
"""Bulk-add Cantex wallets from a pasted key dump.

Input format — one wallet is FIVE non-empty lines (blank lines between wallets
are optional and ignored). The two key lines accept any of three label styles:

    <name>
    <24-word mnemonic>            # ignored — the bot signs with the hex keys
    Cantex::<address>             # kept as a comment for identification only
    <operator key hex>            # or "op: <hex>"      / "operator key: <hex>"
    <trading key hex>             # or "tk: <hex>"      / "trading key: <hex>"

For every wallet it appends CANTEX_<NAME>_OPERATOR_KEY / _TRADING_KEY to .env and
a matching `[[wallets]] name = "<name>"` block to config.toml. Names are
normalised (spaces/dashes -> "_", lowercased, leading "cantex_" dropped).
Idempotent — names already present are skipped. Key VALUES are never printed.

Usage:
    python scripts/add_wallets.py wallets.txt
    python scripts/add_wallets.py wallets.txt --env .env --config config.toml
    python scripts/add_wallets.py wallets.txt --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from cantex_bot.wallet_import import (
        WalletImportError, append_wallets, config_names, env_names, parse_dump,
    )
except ModuleNotFoundError:  # running from a source checkout without install
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from cantex_bot.wallet_import import (
        WalletImportError, append_wallets, config_names, env_names, parse_dump,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Bulk-add Cantex wallets to .env + config.toml")
    ap.add_argument("input", help="text file with the wallet dump")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--dry-run", action="store_true", help="parse + report, write nothing")
    args = ap.parse_args()

    try:
        wallets = parse_dump(Path(args.input).read_text())
    except (OSError, WalletImportError) as exc:
        raise SystemExit(str(exc))

    if args.dry_run:
        have_env = env_names(args.env)
        have_cfg = config_names(args.config)
        env_add = sum(1 for w in wallets if w.name.upper() not in have_env)
        cfg_add = sum(1 for w in wallets if w.name not in have_cfg)
    else:
        try:
            added_env, added_cfg = append_wallets(
                wallets, env_path=args.env, config_path=args.config)
        except WalletImportError as exc:
            raise SystemExit(str(exc))
        env_add, cfg_add = len(added_env), len(added_cfg)

    tag = "  (DRY RUN — nothing written)" if args.dry_run else ""
    print(f"parsed {len(wallets)} wallets{tag}")
    print(f"  .env       : +{env_add} added, {len(wallets) - env_add} already present")
    print(f"  config.toml: +{cfg_add} added, {len(wallets) - cfg_add} already present")
    print("  names:", ", ".join(w.name for w in wallets))


if __name__ == "__main__":
    main()
