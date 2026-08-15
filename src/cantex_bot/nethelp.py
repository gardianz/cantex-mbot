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
import socket

import aiohttp

# Must comfortably exceed PerformanceConfig.refresh_interval (default 30s). At
# keepalive == refresh_interval every pooled connection expires just as the next
# sweep needs it, so every sweep opens fresh sockets and pays the WSL2 SYN stall
# — which is how a stable host still produced "timed out after 4 attempts".
_KEEPALIVE = 120

_shared: aiohttp.TCPConnector | None = None
_shared_loop: asyncio.AbstractEventLoop | None = None


def make_connector(limit: int = 10, limit_per_host: int = 0) -> aiohttp.TCPConnector:
    """A private pool. Prefer ``shared_connector`` for anything per-wallet."""
    return aiohttp.TCPConnector(
        family=socket.AF_INET,     # IPv4 only — no AAAA-lookup stalls
        ttl_dns_cache=300,
        limit=limit,
        limit_per_host=limit_per_host,
        keepalive_timeout=_KEEPALIVE,
        enable_cleanup_closed=True,
    )


def shared_connector(limit: int = 64, limit_per_host: int = 32) -> aiohttp.TCPConnector:
    """One connection pool for every Cantex client in the process.

    A pool per wallet cannot reuse anything: with hundreds of wallets each
    holding its own SDK and web session, every sweep opens a cold socket per
    wallet and hits the WSL2 SYN stall, while nothing caps the total in flight.
    Sharing one pool means warm connections are handed between wallets and
    ``limit``/``limit_per_host`` are a real ceiling on concurrent sockets.

    Sessions built on this MUST pass ``connector_owner=False``, or the first
    session to close takes the pool down with it.
    """
    global _shared, _shared_loop
    loop = asyncio.get_running_loop()
    # A connector is bound to the loop that created it; rebuild if the loop
    # changed (tests, or a second asyncio.run in one process).
    if _shared is None or _shared.closed or _shared_loop is not loop:
        _shared = make_connector(limit=limit, limit_per_host=limit_per_host)
        _shared_loop = loop
    return _shared


async def close_shared_connector() -> None:
    """Close the shared pool. Call after every session using it is closed."""
    global _shared, _shared_loop
    connector, _shared, _shared_loop = _shared, None, None
    if connector is not None and not connector.closed:
        await connector.close()


def make_timeout() -> aiohttp.ClientTimeout:
    # Short connect ceiling so a stalled SYN fails fast and retries; generous
    # read so a slow-but-alive response is not killed.
    return aiohttp.ClientTimeout(total=45, sock_connect=8, sock_read=30)
