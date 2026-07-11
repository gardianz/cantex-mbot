"""Interactive CLI: the human-facing entrypoint tying everything together."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from decimal import Decimal, InvalidOperation

import questionary
from rich.console import Console

from . import logging_setup
from .ccview import CCViewClient
from .config import AppConfig, ConfigError, load_config
from .dashboard import Dashboard
from .guards import SwapGuard
from .markets import MarketError, MarketMap
from .portfolio import PortfolioService
from .runstate import RunState
from .scheduler import StrategyScheduler
from .store import Store
from .strategies.strategy1 import Strategy1
from .swap_all import AmountError, AmountSpec, swap_selected
from .swapper import SwapEngine
from .telegram import TelegramNotifier
from .wallets import WalletManager

logger = logging.getLogger(__name__)
console = Console()


class App:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.store = Store()
        self.manager = WalletManager(config)
        self.guard = SwapGuard(config.guards)
        self.notifier = TelegramNotifier(config.telegram)
        self.engine = SwapEngine(
            self.guard, self.store, self.notifier, dry_run=config.network.dry_run
        )
        self.ccview = CCViewClient(base=config.dashboard.ccview_base_url)
        self.run_state = RunState()
        self.service = PortfolioService(
            self.manager, self.store, self.ccview,
            usdcx_symbol=config.strategy1.usdcx_symbol,
            cc_symbol=config.strategy1.cc_symbol,
            interval=config.performance.refresh_interval,
            run_state=self.run_state,
        )

    # -- lifecycle -----------------------------------------------------------

    async def startup(self) -> None:
        """Print the banner. Auth is lazy — the menu appears immediately and
        each wallet authenticates on first use (dashboard, swap, etc.)."""
        net = self.config.network
        console.rule("[bold]Cantex Bot")
        console.print(
            f"Network: [bold]{net.base_url}[/bold]  |  "
            f"default dry_run: [bold]{net.dry_run}[/bold]  |  "
            f"wallets: {len(self.manager.names)}  |  "
            f"max concurrency: {self.config.performance.max_concurrency}"
        )
        if not net.dry_run:
            console.print("[bold red]LIVE mode configured — real funds at risk.[/bold red]")

    async def shutdown(self) -> None:
        await self.service.stop()
        await self.manager.close()
        await self.notifier.close()
        await self.ccview.close()
        self.store.close()

    # -- helpers -------------------------------------------------------------

    async def _choose_execution_mode(self) -> None:
        """Set engine.dry_run for the next action, defaulting to the safe config."""
        self.engine.dry_run = self.config.network.dry_run
        mode = await questionary.select(
            "Execution mode?",
            choices=["DRY-RUN (safe, no real swaps)", "LIVE (real funds)"],
            default="DRY-RUN (safe, no real swaps)",
        ).ask_async()
        if mode and mode.startswith("LIVE"):
            confirm = await questionary.text(
                "Type LIVE (uppercase) to arm real-money execution:"
            ).ask_async()
            self.engine.dry_run = confirm != "LIVE"
            if self.engine.dry_run:
                console.print("[yellow]Not armed — running DRY-RUN.[/yellow]")
            else:
                console.print("[bold red]LIVE armed.[/bold red]")
        else:
            self.engine.dry_run = True

    async def _run_cancellable(self, coro_factory) -> None:
        """Run an async task, letting Ctrl-C stop it and return to the menu."""
        stop = asyncio.Event()
        task = asyncio.create_task(coro_factory(stop))
        try:
            await task
        except (KeyboardInterrupt, asyncio.CancelledError):
            console.print("\n[yellow]Stopping...[/yellow]")
            stop.set()
            with contextlib.suppress(Exception):
                await task

    # -- menu actions --------------------------------------------------------

    async def action_dashboard(self) -> None:
        dash = Dashboard(self.service, self.config, self.run_state)
        await self._run_cancellable(dash.run)

    async def _tradeable_tokens(self) -> list[str] | None:
        """USDCX-tradeable token symbols (minus CC), from the first wallet."""
        usdcx = self.config.strategy1.usdcx_symbol
        cc = self.config.strategy1.cc_symbol
        try:
            first = self.manager.get(self.manager.names[0])
            await first.ensure_auth()
            market = await MarketMap.build(first.sdk)
            return [p.token_symbol for p in market.trade_pairs(usdcx, exclude_symbols=(cc,))]
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Cannot load market: {exc}[/red]")
            return None

    async def action_strategy1(self) -> None:
        mode = await questionary.select(
            "Strategy 1 run mode?",
            choices=["Run once (today)", "Run daily (loop)", "Back"],
        ).ask_async()
        if not mode or mode == "Back":
            return

        # Choose which USDCX<->token pairs to cycle (default: all).
        syms = await self._tradeable_tokens()
        if not syms:
            console.print("[red]No tradeable tokens found.[/red]")
            return
        tokens = await questionary.checkbox(
            "Pairs to trade (USDCX <-> token; space toggle, enter confirm):",
            choices=[questionary.Choice(s, checked=True) for s in syms],
        ).ask_async()
        if not tokens:
            console.print("[yellow]No pair selected — nothing to run.[/yellow]")
            return
        console.print(f"Trading {len(tokens)} pair(s): {', '.join(tokens)}")

        await self._choose_execution_mode()
        strategy = Strategy1(
            self.manager, self.engine, self.config.strategy1, self.notifier, self.store,
            run_state=self.run_state, tokens=tokens,
        )
        scheduler = StrategyScheduler(strategy, self.notifier)
        runner = scheduler.run_daily if mode.startswith("Run daily") else scheduler.run_once

        # Run the strategy in the background and drop straight into the live
        # dashboard so progress is visible. Ctrl-C stops both and returns.
        strat_stop = asyncio.Event()
        strat_task = asyncio.create_task(runner(strat_stop))
        console.print("[dim]Strategy running — live dashboard below. Ctrl-C to stop.[/dim]")
        dash = Dashboard(self.service, self.config, self.run_state)
        try:
            await self._run_cancellable(dash.run)
        finally:
            strat_stop.set()
            with contextlib.suppress(Exception):
                await strat_task

    async def action_swap_all(self) -> None:
        # 1. Which wallets (1, several, or all).
        wallet_names = await questionary.checkbox(
            "Wallets to use (space to toggle, enter to confirm):",
            choices=[questionary.Choice(n, checked=False) for n in self.manager.names],
        ).ask_async()
        if not wallet_names:
            console.print("[yellow]No wallet selected.[/yellow]")
            return

        # 2. Direction.
        direction = await questionary.select(
            "Direction?",
            choices=[
                "buy  (USDCX -> token)",
                "sell (token -> USDCX)",
                "Back",
            ],
        ).ask_async()
        if not direction or direction == "Back":
            return
        dir_key = "buy" if direction.startswith("buy") else "sell"

        # 3. Which tokens — load the market from the first selected wallet.
        usdcx_sym = self.config.strategy1.usdcx_symbol
        cc_sym = self.config.strategy1.cc_symbol
        try:
            first = self.manager.get(wallet_names[0])
            await first.ensure_auth()
            market = await MarketMap.build(first.sdk)
            token_syms = [
                p.token_symbol
                for p in market.trade_pairs(usdcx_sym, exclude_symbols=(cc_sym,))
            ]
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Cannot load market: {exc}[/red]")
            return
        if not token_syms:
            console.print("[red]No tradeable tokens found.[/red]")
            return
        tokens = await questionary.checkbox(
            f"Tokens to {dir_key} (space to toggle):",
            choices=[questionary.Choice(s) for s in token_syms],
        ).ask_async()
        if not tokens:
            console.print("[yellow]No token selected.[/yellow]")
            return

        # 4. Amount — absolute or percent of balance.
        base = usdcx_sym if dir_key == "buy" else "token"
        amount_raw = await questionary.text(
            f"Amount per swap ({base}) — value (e.g. 5) or percent (e.g. 25%):",
            default="100%" if dir_key == "sell" else "",
        ).ask_async()
        try:
            amount = AmountSpec.parse(amount_raw or "")
        except AmountError as exc:
            console.print(f"[red]Bad amount: {exc}[/red]")
            return

        # 5. Confirm + arm, then run.
        console.print(
            f"[cyan]{dir_key.upper()}[/cyan] {amount} on {len(tokens)} token(s) "
            f"x {len(wallet_names)} wallet(s): {', '.join(tokens)}"
        )
        await self._choose_execution_mode()
        results = await swap_selected(
            self.manager, self.engine,
            wallet_names=wallet_names, token_symbols=tokens,
            usdcx_symbol=usdcx_sym, direction=dir_key, amount=amount,
        )
        for wallet, outcomes in results.items():
            ok = sum(1 for o in outcomes if o.ok)
            console.print(f"[{wallet}] {ok}/{len(outcomes)} ok")

    async def action_manual_swap(self) -> None:
        name = await questionary.select("Wallet?", choices=self.manager.names).ask_async()
        if not name:
            return
        wallet = self.manager.get(name)
        try:
            await wallet.ensure_auth()
            market = await MarketMap.build(wallet.sdk)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Cannot load market: {exc}[/red]")
            return
        sell_sym = (await questionary.text("Sell symbol (e.g. USDCX):").ask_async() or "").strip()
        buy_sym = (await questionary.text("Buy symbol (e.g. cBTC):").ask_async() or "").strip()
        amount_s = (await questionary.text("Sell amount:").ask_async() or "").strip()
        try:
            sell = market.instrument(sell_sym)
            buy = market.instrument(buy_sym)
            amount = Decimal(amount_s)
        except (MarketError, InvalidOperation) as exc:
            console.print(f"[red]Invalid input: {exc}[/red]")
            return
        await self._choose_execution_mode()
        out = await self.engine.execute_swap(
            wallet, sell=sell, buy=buy, sell_amount=amount,
            sell_symbol=sell_sym.upper(), buy_symbol=buy_sym.upper(), direction="manual",
        )
        if out.reject_reasons:
            console.print(f"[red]Guard reject: {'; '.join(out.reject_reasons)}[/red]")
        elif out.error:
            console.print(f"[red]{out.error}[/red]")
        else:
            console.print(f"[green]OK[/green] -> {out.buy_amount} {buy_sym.upper()}")

    async def action_web_check(self) -> None:
        """Report each wallet's data reads — all via the operator key, no cookie."""
        from .ccview import window
        from .webclient import WebClient

        for name in self.manager.names:
            wallet = self.manager.get(name)
            try:
                await wallet.ensure_auth()
            except Exception as exc:  # noqa: BLE001
                console.print(f"[{name}] [red]auth failed: {exc}[/red]")
                continue
            # History (Bearer)
            try:
                trades = await wallet.web.fetch_trading_history()
                today = WebClient.count_today(trades)
                console.print(
                    f"[{name}] [green]history[/green] — {len(trades)} trades "
                    f"({today} today)"
                )
            except Exception as exc:  # noqa: BLE001
                console.print(f"[{name}] [red]history failed: {exc}[/red]")
            # Rebates / weekly reward (Bearer)
            try:
                r = await wallet.web.fetch_rebates()
                console.print(
                    f"[{name}] [green]rebates[/green] — y={r.yesterday} "
                    f"tw={r.this_week} lw={r.last_week} ({r.last_week_status or '—'})"
                )
            except Exception as exc:  # noqa: BLE001
                console.print(f"[{name}] [red]rebates failed: {exc}[/red]")
            # Fee (ccview, anon session)
            try:
                addr = (await wallet.sdk.get_account_info()).address
                s7, e = window(7)
                pf = await self.ccview.party_fee(addr, s7, e)
                console.print(
                    f"[{name}] [green]fee 7d[/green] — {pf.fee} CC [dim](ccview)[/dim]"
                )
            except Exception as exc:  # noqa: BLE001
                console.print(f"[{name}] [red]ccview fee failed: {exc}[/red]")

    async def action_wallet_status(self) -> None:
        with console.status("[cyan]Refreshing portfolio...[/cyan]"):
            dash = Dashboard(self.service, self.config, self.run_state)
            await dash.print_once()

    # -- main loop -----------------------------------------------------------

    async def menu(self) -> None:
        actions = {
            "Dashboard (live)": self.action_dashboard,
            "Strategy 1": self.action_strategy1,
            "Swap 1x all pairs": self.action_swap_all,
            "Manual swap": self.action_manual_swap,
            "Web check (history + rebates)": self.action_web_check,
            "Wallet status": self.action_wallet_status,
            "Quit": None,
        }
        while True:
            choice = await questionary.select(
                "Main menu", choices=list(actions)
            ).ask_async()
            if choice is None or choice == "Quit":
                return
            handler = actions[choice]
            try:
                await handler()
            except Exception as exc:  # noqa: BLE001 - never crash the menu
                logger.exception("Action %s failed", choice)
                console.print(f"[red]Error: {exc}[/red]")


async def amain() -> None:
    logging_setup.setup()
    try:
        config = load_config()
    except ConfigError as exc:
        console.print(f"[red]Config error:[/red] {exc}")
        return
    app = App(config)
    try:
        await app.startup()
        await app.menu()
    finally:
        await app.shutdown()
        console.print("Bye.")


def run() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
