"""Bulk wallet import: parse a pasted key dump into .env + config.toml entries.

Shared by ``scripts/add_wallets.py`` (CLI) and the interactive menu
(``App.action_add_wallets``). One wallet is FIVE non-empty lines; the two key
lines accept ``op:``/``operator key:`` and ``tk:``/``trading key:`` labels or no
label at all::

    <name>
    <24-word mnemonic>            # ignored — the bot signs with the hex keys
    Cantex::<address>             # kept as a comment for identification only
    <operator key hex>
    <trading key hex>
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

HEX64 = re.compile(r"([0-9a-fA-F]{64})")
_NAME_OK = re.compile(r"[a-z0-9_]+")


class WalletImportError(Exception):
    """Bad wallet dump, or an unsafe (non-gitignored) write target."""


@dataclass(frozen=True)
class WalletEntry:
    name: str
    address: str
    operator: str
    trading: str


def normalise_name(raw: str) -> str:
    """Config-safe wallet name: spaces/dashes -> '_', lowercased, leading
    'cantex_' label dropped ('cantex danu_one' -> 'danu_one')."""
    n = re.sub(r"[\s\-]+", "_", raw.strip()).lower()
    n = re.sub(r"^cantex_", "", n)
    if not _NAME_OK.fullmatch(n):
        raise WalletImportError(f"bad wallet name {raw!r} -> {n!r} (need [a-z0-9_])")
    return n


def _hex(line: str, kind: str, block: int) -> str:
    m = HEX64.search(line)
    if not m:
        raise WalletImportError(f"block {block}: no 64-char hex {kind} key in: {line!r}")
    return m.group(1).lower()


def parse_dump(text: str) -> list[WalletEntry]:
    """Parse a key dump into WalletEntry rows. Raises WalletImportError on any
    malformed / misaligned block."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise WalletImportError("empty input")
    if len(lines) % 5 != 0:
        raise WalletImportError(
            f"expected blocks of 5 non-empty lines, got {len(lines)} "
            "(not a multiple of 5) — check the input for a missing/extra line"
        )
    wallets: list[WalletEntry] = []
    for i in range(0, len(lines), 5):
        b = i // 5 + 1
        name, mnem, addr, op_line, tk_line = lines[i:i + 5]
        if not addr.startswith("Cantex::"):
            raise WalletImportError(f"block {b}: line 3 is not a Cantex:: address: {addr!r}")
        if len(mnem.split()) < 12:
            raise WalletImportError(f"block {b}: line 2 is not a mnemonic: {mnem!r}")
        wallets.append(WalletEntry(
            name=normalise_name(name), address=addr,
            operator=_hex(op_line, "operator", b), trading=_hex(tk_line, "trading", b),
        ))
    names = [w.name for w in wallets]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        raise WalletImportError(f"duplicate wallet names in input: {dups}")
    return wallets


def env_names(env_path: str) -> set[str]:
    p = Path(env_path)
    if not p.exists():
        return set()
    return {n.upper() for n in re.findall(
        r"^CANTEX_([A-Z0-9_]+)_OPERATOR_KEY", p.read_text(), re.M)}


def config_names(config_path: str) -> set[str]:
    p = Path(config_path)
    if not p.exists():
        return set()
    return set(re.findall(r'name\s*=\s*"([^"]+)"', p.read_text()))


def assert_gitignored(path: str) -> None:
    """Refuse to write secrets to a file git would track."""
    try:
        r = subprocess.run(["git", "check-ignore", "-q", path], capture_output=True)
    except FileNotFoundError:
        return  # no git available — cannot check, don't block
    if r.returncode == 1:  # 0 = ignored, 1 = NOT ignored, 128 = not a repo
        raise WalletImportError(
            f"refusing to write: {path} is NOT gitignored — add it to .gitignore "
            "first so secrets are never committed"
        )


def append_wallets(
    wallets: list[WalletEntry], *, env_path: str = ".env",
    config_path: str = "config.toml",
) -> tuple[list[WalletEntry], list[WalletEntry]]:
    """Append new wallets to ``env_path`` + ``config_path``. Idempotent (skips
    names already present). Returns ``(added_to_env, added_to_config)``. Never
    logs key values."""
    have_env = env_names(env_path)
    have_cfg = config_names(config_path)
    env_add = [w for w in wallets if w.name.upper() not in have_env]
    cfg_add = [w for w in wallets if w.name not in have_cfg]

    if env_add:
        assert_gitignored(env_path)
        with open(env_path, "a") as f:
            for w in env_add:
                up = w.name.upper()
                f.write(f"\n# {w.name}  {w.address}\n")
                f.write(f"CANTEX_{up}_OPERATOR_KEY={w.operator}\n")
                f.write(f"CANTEX_{up}_TRADING_KEY={w.trading}\n")
    if cfg_add:
        assert_gitignored(config_path)
        with open(config_path, "a") as f:
            for w in cfg_add:
                f.write(f'\n[[wallets]]\nname = "{w.name}"\n')
    return env_add, cfg_add
