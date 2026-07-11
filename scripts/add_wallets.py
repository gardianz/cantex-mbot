#!/usr/bin/env python3
"""Bulk-add Cantex wallets from a pasted key dump.

Input format — one wallet is FIVE non-empty lines (blank lines between wallets
are optional and ignored). The two key lines accept any of three label styles:

    <name>
    <24-word mnemonic>            # ignored — the bot signs with the hex keys
    Cantex::<address>             # kept as a comment for identification only
    <operator key hex>            # or "op: <hex>"      / "operator key: <hex>"
    <trading key hex>             # or "tk: <hex>"      / "trading key: <hex>"

For every wallet it appends to .env:

    CANTEX_<NAME>_OPERATOR_KEY=<hex>
    CANTEX_<NAME>_TRADING_KEY=<hex>

and a matching block to config.toml:

    [[wallets]]
    name = "<name>"

Names are normalised: spaces/dashes -> "_", lowercased, and a leading "cantex_"
label is stripped ("cantex danu_one" -> "danu_one"). Idempotent — a wallet whose
name is already present in the target file is skipped. Key VALUES are never
printed; only names and counts.

Usage:
    python scripts/add_wallets.py wallets.txt
    python scripts/add_wallets.py wallets.txt --env .env --config config.toml
    python scripts/add_wallets.py wallets.txt --dry-run
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HEX64 = re.compile(r"([0-9a-fA-F]{64})")
_NAME_OK = re.compile(r"[a-z0-9_]+")


def normalise_name(raw: str) -> str:
    n = re.sub(r"[\s\-]+", "_", raw.strip()).lower()
    n = re.sub(r"^cantex_", "", n)  # drop the "cantex " label prefix
    if not _NAME_OK.fullmatch(n):
        raise SystemExit(f"bad wallet name {raw!r} -> {n!r} (need [a-z0-9_])")
    return n


def _hex(line: str, kind: str, block: int) -> str:
    m = HEX64.search(line)
    if not m:
        raise SystemExit(f"block {block}: no 64-char hex {kind} key in: {line!r}")
    return m.group(1).lower()


def parse(text: str) -> list[dict]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) % 5 != 0:
        raise SystemExit(
            f"expected blocks of 5 non-empty lines, got {len(lines)} "
            "(not a multiple of 5) — check the input for a missing/extra line"
        )
    wallets: list[dict] = []
    for i in range(0, len(lines), 5):
        b = i // 5 + 1
        name, mnem, addr, op_line, tk_line = lines[i:i + 5]
        if not addr.startswith("Cantex::"):
            raise SystemExit(f"block {b}: line 3 is not a Cantex:: address: {addr!r}")
        if len(mnem.split()) < 12:
            raise SystemExit(f"block {b}: line 2 is not a mnemonic: {mnem!r}")
        wallets.append({
            "name": normalise_name(name),
            "address": addr,
            "operator": _hex(op_line, "operator", b),
            "trading": _hex(tk_line, "trading", b),
        })
    names = [w["name"] for w in wallets]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        raise SystemExit(f"duplicate wallet names in input: {dups}")
    return wallets


def _names_in(path: str, pattern: str) -> set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    return set(re.findall(pattern, p.read_text(), re.M))


def _assert_gitignored(path: str) -> None:
    """Refuse to write secrets to a file git would track."""
    try:
        r = subprocess.run(["git", "check-ignore", "-q", path],
                           capture_output=True)
    except FileNotFoundError:
        print(f"warning: git not found — cannot confirm {path} is gitignored")
        return
    # exit 0 = ignored, 1 = NOT ignored, 128 = not a git repo
    if r.returncode == 1:
        raise SystemExit(
            f"refusing to write: {path} is NOT gitignored — add it to "
            ".gitignore first so secrets are never committed"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Bulk-add Cantex wallets to .env + config.toml")
    ap.add_argument("input", help="text file with the wallet dump")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--dry-run", action="store_true", help="parse + report, write nothing")
    args = ap.parse_args()

    wallets = parse(Path(args.input).read_text())

    env_have = {n.upper() for n in _names_in(args.env, r"^CANTEX_([A-Z0-9_]+)_OPERATOR_KEY")}
    cfg_have = _names_in(args.config, r'name\s*=\s*"([^"]+)"')

    env_add = [w for w in wallets if w["name"].upper() not in env_have]
    cfg_add = [w for w in wallets if w["name"] not in cfg_have]

    if not args.dry_run:
        if env_add:
            _assert_gitignored(args.env)
            with open(args.env, "a") as f:
                for w in env_add:
                    up = w["name"].upper()
                    f.write(f"\n# {w['name']}  {w['address']}\n")
                    f.write(f"CANTEX_{up}_OPERATOR_KEY={w['operator']}\n")
                    f.write(f"CANTEX_{up}_TRADING_KEY={w['trading']}\n")
        if cfg_add:
            _assert_gitignored(args.config)
            with open(args.config, "a") as f:
                for w in cfg_add:
                    f.write(f'\n[[wallets]]\nname = "{w["name"]}"\n')

    print(f"parsed {len(wallets)} wallets" + ("  (DRY RUN — nothing written)" if args.dry_run else ""))
    print(f"  .env      : +{len(env_add)} added, {len(wallets) - len(env_add)} already present")
    print(f"  config.toml: +{len(cfg_add)} added, {len(wallets) - len(cfg_add)} already present")
    print("  names:", ", ".join(w["name"] for w in wallets))


if __name__ == "__main__":
    main()
