"""Shared aiohttp connector/timeout tuned for a flaky WSL2/NAT path.

Findings: raw TCP connect to the host is ~0.2s and stable, but WSL2 occasionally
stalls a fresh connection's SYN for many seconds. So: pin IPv4 (skip AAAA
stalls), cache DNS, keep connections alive and reuse them, proactively clean up
half-closed sockets, and use a SHORT connect timeout so a stalled connect fails
fast and the retry (usually a good path) recovers in ~1-2s instead of ~20s.
"""
from __future__ import annotations

import socket

import aiohttp


def make_connector(limit: int = 10) -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(
        family=socket.AF_INET,     # IPv4 only — no AAAA-lookup stalls
        ttl_dns_cache=300,
        limit=limit,
        keepalive_timeout=30,
        enable_cleanup_closed=True,
    )


def make_timeout() -> aiohttp.ClientTimeout:
    # Short connect ceiling so a stalled SYN fails fast and retries; generous
    # read so a slow-but-alive response is not killed.
    return aiohttp.ClientTimeout(total=45, sock_connect=8, sock_read=30)
