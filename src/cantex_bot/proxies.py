"""Per-wallet proxy list, read from ``proxies.txt``.

One proxy per wallet, so each account's traffic leaves from its own IP. Line N
serves wallet N in config order; if there are fewer proxies than wallets the
list wraps, and wallets sharing a proxy also share its connection pool.

Accepted line formats (``#`` comments and blank lines ignored)::

    http://user:pass@host:8080      full URL
    socks5://127.0.0.1:40000        full URL
    host:8080                       bare host:port      -> http://
    host:8080:user:pass             host:port:user:pass -> http://user:pass@...
    user:pass@host:8080             credentials first   -> http://

Every scheme is applied at the *connector*, never via ``HTTP(S)_PROXY``: the env
var is process-wide, so it cannot express a different proxy per wallet. That
means a proxy list needs the ``socks`` extra even for plain http:// entries —
``aiohttp_socks.ProxyConnector`` is what handles all of them uniformly.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PROXY_FILE = "proxies.txt"
_SCHEMES = ("http", "https", "socks4", "socks5", "socks5h")


class ProxyFileError(Exception):
    """proxies.txt missing, empty, or holding a line we cannot parse."""


def normalise(line: str, *, lineno: int | None = None) -> str:
    """One raw line -> a full proxy URL. Raises on anything unparseable."""
    where = f" (line {lineno})" if lineno else ""
    s = line.strip()
    if "://" in s:
        scheme, rest = s.split("://", 1)
        scheme = scheme.lower()
    else:
        scheme, rest = "http", s
    if scheme not in _SCHEMES:
        raise ProxyFileError(
            f"unsupported proxy scheme {scheme!r}{where}: "
            f"expected one of {', '.join(_SCHEMES)}"
        )
    if not rest:
        raise ProxyFileError(f"empty proxy address{where}")
    if "@" in rest:                       # user:pass@host:port — already ordered
        return f"{scheme}://{rest}"
    parts = rest.split(":")
    if len(parts) == 2:                   # host:port
        return f"{scheme}://{rest}"
    if len(parts) == 4:                   # host:port:user:pass
        host, port, user, password = parts
        return f"{scheme}://{user}:{password}@{host}:{port}"
    raise ProxyFileError(
        f"cannot parse proxy {s!r}{where}: expected host:port, "
        "host:port:user:pass, user:pass@host:port, or a full URL"
    )


def parse_proxies(text: str) -> list[str]:
    """Parse a whole proxies.txt body into normalised URLs, in order."""
    out: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(normalise(line, lineno=lineno))
    return out


def load_proxies(path: str | Path = DEFAULT_PROXY_FILE) -> list[str]:
    """Read and parse the proxy file. Raises ProxyFileError if it is missing or
    holds no usable entry — silently running direct would defeat the point."""
    p = Path(path)
    if not p.exists():
        raise ProxyFileError(
            f"proxy file not found: {p}. Copy proxies.example.txt to {p}, "
            "or clear proxy_file in config.toml to connect directly."
        )
    proxies = parse_proxies(p.read_text())
    if not proxies:
        raise ProxyFileError(f"{p} has no proxy entries (only blanks/comments)")
    return proxies


def assign(wallet_names: list[str], proxies: list[str]) -> dict[str, str]:
    """Map wallet -> proxy, line N to wallet N, wrapping when the list is short.

    Wrapping is deliberate: a handful of proxies across many wallets is a normal
    setup, and it keeps the mapping stable across restarts (index-based, not
    round-robin), so a wallet keeps the same egress IP.
    """
    if not proxies:
        return {}
    mapping = {
        name: proxies[i % len(proxies)] for i, name in enumerate(wallet_names)
    }
    if len(proxies) < len(wallet_names):
        logger.info(
            "%d proxies for %d wallets — reusing each about %.1f times",
            len(proxies), len(wallet_names),
            len(wallet_names) / len(proxies),
        )
    return mapping


def redact(url: str) -> str:
    """Proxy URL safe to log: credentials replaced with ``***``."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        rest = "***@" + rest.rsplit("@", 1)[1]
    return f"{scheme}://{rest}"
