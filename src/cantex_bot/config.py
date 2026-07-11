"""Configuration loading for the Cantex bot.

Reads ``config.toml`` for non-secret settings and ``.env`` for private keys
and Telegram credentials. Private keys never live in ``config.toml``.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class NetworkConfig:
    base_url: str = "https://api.cantex.io"
    dry_run: bool = True


@dataclass(frozen=True)
class GuardConfig:
    max_slippage: Decimal = Decimal("1.0")       # percent
    max_pool_fee_pct: Decimal = Decimal("0.3")   # percent
    max_network_fee: Decimal = Decimal("0.5")    # absolute, in CC


@dataclass(frozen=True)
class Strategy1Config:
    cc_units: Decimal = Decimal("5")
    daily_swap_target: int = 20
    usdcx_symbol: str = "USDCX"
    cc_symbol: str = "CC"
    cooldown_seconds: float = 2.0          # pause after an actual swap
    insufficient_retries: int = 4          # stop a wallet after N "saldo kurang" tries
    min_ticket_cc: Decimal = Decimal("10") # exchange min ticket; below this a token holding is dust
    # When a live swap is submitted but its confirmation errors (WSL2 network /
    # WS timeout), the swap may still have settled. Before doing anything else,
    # poll the trading history this many times (this interval apart): a higher
    # today-count proves the swap went through — never fire the opposite leg on a
    # maybe (prevents the buy/sell "collision").
    confirm_retries: int = 5
    confirm_interval: float = 2.0
    # Adaptive fee polling: while waiting for the network fee to drop to
    # max_network_fee, re-quote fast when the fee is close, slow when far.
    poll_min_seconds: float = 0.5          # interval when fee is at/near threshold
    poll_max_seconds: float = 5.0          # interval when fee is far above
    poll_far_ratio: float = 0.3            # fee this fraction above threshold => poll_max


@dataclass(frozen=True)
class SwapAllConfig:
    sell_amount: Decimal = Decimal("1")


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    bot_token: str | None = None
    chat_id: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.enabled and self.bot_token and self.chat_id)


@dataclass(frozen=True)
class DashboardConfig:
    refresh_seconds: float = 5.0
    api_base_url: str = "https://api.cantex.io"
    ccview_base_url: str = "https://ccview.io"


@dataclass(frozen=True)
class PerformanceConfig:
    # Global cap on simultaneous outbound requests across all wallets — the
    # single most important knob at scale (hundreds of wallets would otherwise
    # storm the host and time out). Auth is lazy: a wallet authenticates on
    # first use, not all up front.
    max_concurrency: int = 10
    lazy_auth: bool = True
    # Seconds between background portfolio refresh sweeps.
    refresh_interval: float = 30.0


@dataclass(frozen=True)
class WalletConfig:
    name: str
    operator_key: str
    trading_key: str | None


@dataclass(frozen=True)
class AppConfig:
    network: NetworkConfig
    guards: GuardConfig
    strategy1: Strategy1Config
    swap_all: SwapAllConfig
    telegram: TelegramConfig
    dashboard: DashboardConfig
    performance: PerformanceConfig
    wallets: list[WalletConfig] = field(default_factory=list)


def _dec(value: object, default: Decimal) -> Decimal:
    if value is None:
        return default
    # str() first so a TOML float like 0.5 becomes Decimal("0.5") exactly.
    return Decimal(str(value))


def load_config(
    config_path: str | Path = "config.toml",
    env_path: str | Path | None = ".env",
) -> AppConfig:
    """Load and validate the full application configuration."""
    if env_path is not None and Path(env_path).exists():
        load_dotenv(env_path)
    else:
        load_dotenv()  # fall back to a discovered .env / process env

    path = Path(config_path)
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.toml to config.toml."
        )
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    net = raw.get("network", {})
    network = NetworkConfig(
        base_url=net.get("base_url", NetworkConfig.base_url),
        dry_run=bool(net.get("dry_run", True)),
    )

    g = raw.get("guards", {})
    guards = GuardConfig(
        max_slippage=_dec(g.get("max_slippage"), GuardConfig.max_slippage),
        max_pool_fee_pct=_dec(g.get("max_pool_fee_pct"), GuardConfig.max_pool_fee_pct),
        max_network_fee=_dec(g.get("max_network_fee"), GuardConfig.max_network_fee),
    )

    s1 = raw.get("strategy1", {})
    strategy1 = Strategy1Config(
        cc_units=_dec(s1.get("cc_units"), Strategy1Config.cc_units),
        daily_swap_target=int(s1.get("daily_swap_target", 20)),
        usdcx_symbol=s1.get("usdcx_symbol", "USDCX"),
        cc_symbol=s1.get("cc_symbol", "CC"),
        cooldown_seconds=float(s1.get("cooldown_seconds", 2.0)),
        insufficient_retries=int(s1.get("insufficient_retries", 4)),
        min_ticket_cc=_dec(s1.get("min_ticket_cc"), Strategy1Config.min_ticket_cc),
        confirm_retries=int(s1.get("confirm_retries", 5)),
        confirm_interval=float(s1.get("confirm_interval", 2.0)),
        poll_min_seconds=float(s1.get("poll_min_seconds", 0.5)),
        poll_max_seconds=float(s1.get("poll_max_seconds", 5.0)),
        poll_far_ratio=float(s1.get("poll_far_ratio", 0.3)),
    )

    sa = raw.get("swap_all", {})
    swap_all = SwapAllConfig(
        sell_amount=_dec(sa.get("sell_amount"), SwapAllConfig.sell_amount),
    )

    tg = raw.get("telegram", {})
    telegram = TelegramConfig(
        enabled=bool(tg.get("enabled", False)),
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        chat_id=os.getenv("TELEGRAM_CHAT_ID"),
    )

    db = raw.get("dashboard", {})
    dashboard = DashboardConfig(
        refresh_seconds=float(db.get("refresh_seconds", 5.0)),
        api_base_url=db.get("api_base_url", DashboardConfig.api_base_url),
        ccview_base_url=db.get("ccview_base_url", DashboardConfig.ccview_base_url),
    )

    pf = raw.get("performance", {})
    performance = PerformanceConfig(
        max_concurrency=int(pf.get("max_concurrency", 10)),
        lazy_auth=bool(pf.get("lazy_auth", True)),
        refresh_interval=float(pf.get("refresh_interval", 30.0)),
    )

    wallets: list[WalletConfig] = []
    for w in raw.get("wallets", []):
        name = w.get("name")
        if not name:
            raise ConfigError("Each [[wallets]] entry needs a 'name'.")
        env_prefix = f"CANTEX_{name.upper()}"
        operator_key = os.getenv(f"{env_prefix}_OPERATOR_KEY")
        trading_key = os.getenv(f"{env_prefix}_TRADING_KEY")
        if not operator_key:
            raise ConfigError(
                f"Missing {env_prefix}_OPERATOR_KEY in .env for wallet '{name}'."
            )
        wallets.append(
            WalletConfig(
                name=name,
                operator_key=operator_key,
                trading_key=trading_key,
            )
        )

    if not wallets:
        raise ConfigError("No wallets configured. Add at least one [[wallets]] block.")

    return AppConfig(
        network=network,
        guards=guards,
        strategy1=strategy1,
        swap_all=swap_all,
        telegram=telegram,
        dashboard=dashboard,
        performance=performance,
        wallets=wallets,
    )
