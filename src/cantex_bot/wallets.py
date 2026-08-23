"""Wallet management: build and authenticate a CantexSDK per wallet."""
from __future__ import annotations

import asyncio
import logging

from cantex_sdk import CantexSDK, IntentTradingKeySigner, OperatorKeySigner

from .config import AppConfig, WalletConfig
from .nethelp import connector_for, make_timeout
from .proxies import redact
from .webclient import WebClient

logger = logging.getLogger(__name__)

_SDK_TIMEOUT = make_timeout()

# Ceiling on one authenticate(). The SDK retries each of its three auth requests
# four times, so a bad network path can keep it busy for minutes — and it holds
# an asyncio lock throughout, which parks every other caller for that wallet.
# Better to fail the attempt and let the caller retry than to hang for ever.
_AUTH_TIMEOUT = 90.0


class Wallet:
    """A single wallet: config + trading SDK + web reader.

    ``sdk`` (operator/trading keys) is used for swaps. ``web`` reads trading
    history and CC rebates the SDK does not expose — using the SDK's Bearer
    token (operator key), no session cookie.

    Authentication is lazy: ``ensure_auth`` seeds an IPv4 session and
    authenticates on first use, so hundreds of unused wallets cost nothing.
    """

    def __init__(
        self, cfg: WalletConfig, sdk: CantexSDK, web: WebClient,
        proxy: str | None = None,
    ) -> None:
        self.name = cfg.name
        self.cfg = cfg
        self.sdk = sdk
        self.web = web  # history + rebates, both via the operator-key Bearer
        self.proxy = proxy  # this wallet's egress; None = direct
        self.authed = False
        self._auth_lock: asyncio.Lock | None = None

    def _seed_session(self) -> None:
        """Give the SDK an IPv4, DNS-cached, keep-alive session (once).

        The pool is shared by every wallet on the same egress, so a warm
        connection opened for one serves the next — a pool per wallet reuses
        nothing and pays a cold connect (and the WSL2 SYN stall) every sweep.
        """
        import aiohttp
        if self.sdk._session is None or self.sdk._session.closed:
            self.sdk._session = aiohttp.ClientSession(
                timeout=self.sdk._timeout,
                connector=connector_for(self.proxy),
                connector_owner=False,   # the pool outlives this session
                trust_env=True,          # honour HTTP(S)_PROXY / NO_PROXY
                headers={"User-Agent": "CantexSDK/1.0"},
            )

    async def ensure_auth(self) -> None:
        """Authenticate this wallet exactly once, lazily and concurrency-safe."""
        if self.authed:
            return
        if self._auth_lock is None:
            self._auth_lock = asyncio.Lock()
        async with self._auth_lock:
            if self.authed:
                return
            self._seed_session()
            try:
                await asyncio.wait_for(self.sdk.authenticate(),
                                       timeout=_AUTH_TIMEOUT)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"authenticate timed out after {_AUTH_TIMEOUT:.0f}s"
                ) from exc
            self.authed = True

    def __repr__(self) -> str:
        return f"Wallet(name={self.name!r}, authed={self.authed})"


class WalletManager:
    """Owns one CantexSDK per configured wallet."""

    def __init__(self, config: AppConfig, proxy_map: dict[str, str] | None = None) -> None:
        self._config = config
        self.wallets: dict[str, Wallet] = {}
        self.proxy_map = proxy_map or {}
        # Global cap on simultaneous outbound requests across all wallets.
        self.sem = asyncio.Semaphore(config.performance.max_concurrency)
        for wcfg in config.wallets:
            proxy = self.proxy_map.get(wcfg.name)
            operator = OperatorKeySigner.from_hex(wcfg.operator_key)
            intent = (
                IntentTradingKeySigner.from_hex(wcfg.trading_key)
                if wcfg.trading_key
                else None
            )
            sdk = CantexSDK(
                operator,
                intent,
                base_url=config.network.base_url,
                api_key_path=f"secrets/{wcfg.name}_api_key.txt",
                timeout=_SDK_TIMEOUT,
                max_retries=4,
                retry_base_delay=1.0,
            )
            # History + rebates both read via the SDK's Bearer token (operator
            # key) — no session cookie needed.
            web = WebClient(
                token_provider=sdk.authenticate,
                api_base=config.dashboard.api_base_url,
                proxy=proxy,   # same egress as this wallet's SDK session
            )
            self.wallets[wcfg.name] = Wallet(wcfg, sdk, web, proxy=proxy)
            if proxy:
                logger.info("Wallet %s egress: %s", wcfg.name, redact(proxy))

    def get(self, name: str) -> Wallet:
        try:
            return self.wallets[name]
        except KeyError:
            raise KeyError(f"Unknown wallet: {name!r}") from None

    @property
    def names(self) -> list[str]:
        return list(self.wallets)

    async def ensure_auth(self, wallet: Wallet) -> None:
        """Authenticate one wallet lazily, bounded by the global semaphore."""
        if wallet.authed:
            return
        async with self.sem:
            await wallet.ensure_auth()

    async def authenticate_all(self) -> dict[str, str | Exception]:
        """Authenticate every wallet, bounded by the global semaphore.

        Used for the non-lazy startup path. Returns wallet name -> "ok" or the
        raised exception, so one bad wallet does not abort the rest.
        """
        async def _auth(w: Wallet) -> str | Exception:
            try:
                await self.ensure_auth(w)
                logger.info("Wallet %s authenticated", w.name)
                return "ok"
            except Exception as exc:  # noqa: BLE001 - report per-wallet
                logger.error("Wallet %s auth failed: %s", w.name, exc)
                return exc

        results = await asyncio.gather(
            *(_auth(w) for w in self.wallets.values())
        )
        return dict(zip(self.wallets, results))

    async def close(self) -> None:
        """Close every wallet's SDK and web client."""
        coros = []
        for w in self.wallets.values():
            coros.append(w.sdk.close())
            if w.web is not None:
                coros.append(w.web.close())
        await asyncio.gather(*coros, return_exceptions=True)
