"""Shared aiohttp connector/timeout tuned for a flaky WSL2/NAT path.

Findings: raw TCP connect to the host is ~0.2s and stable, but WSL2 occasionally
stalls a fresh connection's SYN for many seconds. So: pin IPv4 (skip AAAA
stalls), cache DNS, keep connections alive and reuse them, proactively clean up
half-closed sockets, and use a SHORT connect timeout so a stalled connect fails
fast and the retry (usually a good path) recovers in ~1-2s instead of ~20s.

Reuse only helps if the pool is actually shared and actually still warm, which
is why ``shared_connector`` exists and why the keep-alive outlives a sweep — see
those two docstrings.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket

import aiohttp

logger = logging.getLogger(__name__)

# Must comfortably exceed PerformanceConfig.refresh_interval (default 30s). At
# keepalive == refresh_interval every pooled connection expires just as the next
# sweep needs it, so every sweep opens fresh sockets and pays the WSL2 SYN stall
# — which is how a stable host still produced "timed out after 4 attempts".
_KEEPALIVE = 120

# One pool per proxy (None = direct). Wallets sharing a proxy share its pool.
_pools: dict[str | None, aiohttp.TCPConnector] = {}
_pools_loop: asyncio.AbstractEventLoop | None = None
_proxy: str | None = None


class ProxyError(Exception):
    """Proxy configured but unusable (bad URL, or missing SOCKS support)."""


def _is_socks(url: str) -> bool:
    return url.split("://", 1)[0].lower().startswith("socks")


def configure_proxy(url: str | None) -> None:
    """Route every outbound request through ``url``. Call once, before any
    session is built (a session already holding the old pool keeps it).

    Two mechanisms, because the SDK builds its own requests and never passes
    ``proxy=``:

    * ``http(s)://`` — exported as ``HTTP(S)_PROXY``. Every session in this bot
      is built with ``trust_env=True``, so aiohttp picks it up for requests it
      was never told about, and honours ``NO_PROXY`` too.
    * ``socks5://`` / ``socks4://`` — applied at the connector instead, which
      the SDK inherits because the bot injects the session it uses. Needs the
      ``socks`` extra (``pip install -e ".[socks]"``).

    Passing an empty value clears any proxy set earlier.
    """
    global _proxy
    url = (url or "").strip()
    if not url:
        _proxy = None
        return
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme not in ("http", "https", "socks4", "socks5", "socks5h"):
        raise ProxyError(
            f"proxy {url!r}: expected http://, https://, socks4:// or socks5://"
        )
    if scheme.startswith("socks"):
        try:
            import aiohttp_socks  # noqa: F401
        except ImportError as exc:
            raise ProxyError(
                f"proxy {url!r} needs SOCKS support: pip install -e \".[socks]\""
            ) from exc
    else:
        os.environ["HTTP_PROXY"] = url
        os.environ["HTTPS_PROXY"] = url
    _proxy = url
    logger.info("Routing outbound requests through proxy %s", url)


def proxy_url() -> str | None:
    """The configured proxy, if any (for banners and diagnostics)."""
    return _proxy


def make_connector(
    limit: int = 10, limit_per_host: int = 0, proxy: str | None = None,
) -> aiohttp.TCPConnector:
    """A private pool, optionally tunnelled through ``proxy``.

    Prefer ``connector_for`` so pools are shared. A proxy is applied here rather
    than via ``proxy=`` on the request, because the SDK builds its own requests
    — and via the connector rather than ``HTTP(S)_PROXY`` when it is per-wallet,
    because that env var is process-wide.
    """
    kwargs = dict(
        family=socket.AF_INET,     # IPv4 only — no AAAA-lookup stalls
        ttl_dns_cache=300,
        limit=limit,
        limit_per_host=limit_per_host,
        keepalive_timeout=_KEEPALIVE,
        enable_cleanup_closed=True,
    )
    if proxy:
        from aiohttp_socks import ProxyConnector
        return ProxyConnector.from_url(proxy, **kwargs)
    return aiohttp.TCPConnector(**kwargs)


def connector_for(
    proxy: str | None = None, *, limit: int = 64, limit_per_host: int = 32,
) -> aiohttp.TCPConnector:
    """The shared connection pool for ``proxy`` (``None`` = direct egress).

    A pool per wallet cannot reuse anything: with hundreds of wallets each
    holding its own SDK and web session, every sweep opens a cold socket per
    wallet and hits the WSL2 SYN stall, while nothing caps the total in flight.
    Pooling by *proxy* keeps that reuse — wallets sharing an egress share warm
    connections — while still giving each egress its own IP and its own ceiling.

    Sessions built on this MUST pass ``connector_owner=False``, or the first
    session to close takes the pool down with every other wallet's connections.
    """
    global _pools_loop
    loop = asyncio.get_running_loop()
    # A connector is bound to the loop that created it; drop them all if the
    # loop changed (tests, or a second asyncio.run in one process).
    if _pools_loop is not loop:
        _pools.clear()
        _pools_loop = loop
    # With no per-wallet proxy, fall back to a single configured SOCKS proxy —
    # a single http:// one already applies through HTTP(S)_PROXY + trust_env.
    key = proxy or (_proxy if _proxy and _is_socks(_proxy) else None)
    pool = _pools.get(key)
    if pool is None or pool.closed:
        pool = make_connector(
            limit=limit, limit_per_host=limit_per_host, proxy=key,
        )
        _pools[key] = pool
    return pool


def shared_connector(limit: int = 64, limit_per_host: int = 32) -> aiohttp.TCPConnector:
    """The direct (no-proxy) pool — see :func:`connector_for`."""
    return connector_for(None, limit=limit, limit_per_host=limit_per_host)


async def close_shared_connector() -> None:
    """Close every pool. Call after all sessions using them are closed."""
    global _pools_loop
    pools, _pools_loop = list(_pools.values()), None
    _pools.clear()
    for pool in pools:
        if not pool.closed:
            await pool.close()


def make_timeout() -> aiohttp.ClientTimeout:
    # Short connect ceiling so a stalled SYN fails fast and retries; generous
    # read so a slow-but-alive response is not killed.
    return aiohttp.ClientTimeout(total=45, sock_connect=8, sock_read=30)
