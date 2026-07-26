"""Interactive CLI: the human-facing entrypoint tying everything together."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

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
from .strategies.strategy2 import Strategy2
from .swap_all import AmountError, AmountSpec, swap_selected
from .swapper import SwapEngine
from .telegram import TelegramCommandBot, TelegramNotifier
from .wallets import WalletManager
from .withdraw import WithdrawError, validate_receiver, withdraw_selected

logger = logging.getLogger(__name__)
console = Console()


def _sig(v: Decimal) -> str:
    """Loss value to 2 decimals, keeping its sign (negative = net gain)."""
    return f"{v:.2f}"


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
        self.tgbot = TelegramCommandBot(
            config.telegram, self.notifier,
            handlers={
                "stats": self._cmd_stats,
                "help": self._cmd_help,
                "start": self._cmd_help,
            },
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
        # If Telegram is configured, run the /stats command bot and keep the
        # background portfolio cache warm so replies have live numbers.
        if self.tgbot.enabled:
            self.service.start()
            self.tgbot.start()
            console.print("[dim]Telegram bot online — send /stats for a per-wallet report.[/dim]")

    async def shutdown(self) -> None:
        await self.tgbot.stop()
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

    async def _first_market(self) -> "MarketMap | None":
        """Build the market map from the first wallet (for token pickers)."""
        try:
            first = self.manager.get(self.manager.names[0])
            await first.ensure_auth()
            return await MarketMap.build(first.sdk)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Cannot load market: {exc}[/red]")
            return None

    async def action_strategy1(self) -> None:
        await self._launch_strategy(Strategy1, "Strategy 1")

    async def action_strategy2(self) -> None:
        await self._launch_strategy(Strategy2, "Strategy 2 (auto lowest-fee)")

    async def _launch_strategy(self, strategy_cls, title: str) -> None:
        """Shared setup for the base<->token strategies: pick run mode, base
        token, candidate pairs, execution mode, then run with the live
        dashboard. ``strategy_cls`` picks targets (Strategy1 = round-robin,
        Strategy2 = lowest fee)."""
        mode = await questionary.select(
            f"{title} run mode?",
            choices=["Run once (today)", "Run daily (loop)", "Back"],
        ).ask_async()
        if not mode or mode == "Back":
            return

        market = await self._first_market()
        if market is None:
            return
        usdcx = self.config.strategy1.usdcx_symbol
        cc = self.config.strategy1.cc_symbol

        # Base token to cycle against (default USDCX; any pool token works —
        # the swap endpoint routes token->token multi-hop via CC).
        bases = [usdcx] + [b for b in market.pool_token_symbols()
                           if b.upper() != usdcx.upper()]
        base_sym = await questionary.select(
            "Base token to cycle against (base <-> token):",
            choices=bases, default=usdcx,
        ).ask_async()
        if not base_sym:
            return

        # Choose the candidate base<->token pairs (default: all). Strategy2 will
        # auto-pick the lowest-fee one of these on each buy.
        syms = [p.token_symbol
                for p in market.trade_pairs(base_sym, exclude_symbols=(cc,))]
        if not syms:
            console.print(f"[red]No tradeable tokens for base {base_sym}.[/red]")
            return
        tokens = await questionary.checkbox(
            f"Candidate pairs ({base_sym} <-> token; space toggle, enter confirm):",
            choices=[questionary.Choice(s, checked=True) for s in syms],
        ).ask_async()
        if not tokens:
            console.print("[yellow]No pair selected — nothing to run.[/yellow]")
            return
        console.print(
            f"{title}: {len(tokens)} pair(s) [base {base_sym}]: {', '.join(tokens)}")

        await self._choose_execution_mode()
        strategy = strategy_cls(
            self.manager, self.engine, self.config.strategy1, self.notifier, self.store,
            run_state=self.run_state, tokens=tokens, base_symbol=base_sym,
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
                "swap (token -> token)",
                "Back",
            ],
        ).ask_async()
        if not direction or direction == "Back":
            return
        dir_key = ("buy" if direction.startswith("buy")
                   else "sell" if direction.startswith("sell") else "swap")

        # 3. Load the market from the first selected wallet.
        usdcx_sym = self.config.strategy1.usdcx_symbol
        cc_sym = self.config.strategy1.cc_symbol
        try:
            first = self.manager.get(wallet_names[0])
            await first.ensure_auth()
            market = await MarketMap.build(first.sdk)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Cannot load market: {exc}[/red]")
            return

        # 4. Pick the sell token (token->token) and the buy token(s).
        sell_sym = None
        if dir_key == "swap":
            all_syms = market.pool_token_symbols()
            if not all_syms:
                console.print("[red]No pool tokens found.[/red]")
                return
            sell_sym = await questionary.select(
                "Sell token (token -> token):", choices=all_syms,
            ).ask_async()
            if not sell_sym:
                return
            choices = [s for s in all_syms if s.upper() != sell_sym.upper()]
            prompt = f"Buy tokens (swap {sell_sym} -> …; space to toggle):"
        else:
            choices = [
                p.token_symbol
                for p in market.trade_pairs(usdcx_sym, exclude_symbols=(cc_sym,))
            ]
            if not choices:
                console.print("[red]No tradeable tokens found.[/red]")
                return
            prompt = f"Tokens to {dir_key} (space to toggle):"
        tokens = await questionary.checkbox(
            prompt, choices=[questionary.Choice(s) for s in choices],
        ).ask_async()
        if not tokens:
            console.print("[yellow]No token selected.[/yellow]")
            return

        # 5. Amount — absolute or percent of balance.
        base = usdcx_sym if dir_key == "buy" else sell_sym if dir_key == "swap" else "token"
        amount_raw = await questionary.text(
            f"Amount per swap ({base}) — value (e.g. 5) or percent (e.g. 25%):",
            default="100%" if dir_key in ("sell", "swap") else "",
        ).ask_async()
        try:
            amount = AmountSpec.parse(amount_raw or "")
        except AmountError as exc:
            console.print(f"[red]Bad amount: {exc}[/red]")
            return

        # 6. Confirm + arm.
        head = (f"SWAP {sell_sym} ->" if dir_key == "swap" else dir_key.upper())
        console.print(
            f"[cyan]{head}[/cyan] {amount} on {len(tokens)} token(s) "
            f"x {len(wallet_names)} wallet(s): {', '.join(tokens)}"
        )
        await self._choose_execution_mode()
        bypass = await questionary.confirm(
            "Bypass ALL guards (fee / slippage / pool fee / network fee) and "
            "execute regardless? Real-money risk.", default=False,
        ).ask_async()
        if bypass:
            console.print("[bold red]Guards BYPASSED for this run.[/bold red]")

        # 7. Run the swaps in the background and drop into the live dashboard so
        # progress is visible; print the per-wallet summary once it closes.
        task = asyncio.create_task(swap_selected(
            self.manager, self.engine,
            wallet_names=wallet_names, token_symbols=tokens,
            usdcx_symbol=usdcx_sym, direction=dir_key, amount=amount,
            sell_symbol=sell_sym, bypass_guards=bool(bypass),
            run_state=self.run_state,
        ))
        console.print("[dim]Swapping — live dashboard below. Ctrl-C/q to return.[/dim]")
        dash = Dashboard(self.service, self.config, self.run_state)
        try:
            await self._run_cancellable(dash.run)
        finally:
            try:
                results = await task
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]Swap run error: {exc}[/red]")
                return
        for wallet, outcomes in results.items():
            ok = sum(1 for o in outcomes if o.ok)
            console.print(f"[{wallet}] {ok}/{len(outcomes)} ok")

    async def action_withdraw(self) -> None:
        """Bulk withdraw: send one token from many wallets to one address."""
        wallet_names = await questionary.checkbox(
            "Wallets to withdraw from (space to toggle, enter to confirm):",
            choices=[questionary.Choice(n, checked=False) for n in self.manager.names],
        ).ask_async()
        if not wallet_names:
            console.print("[yellow]No wallet selected.[/yellow]")
            return

        market = await self._first_market()
        if market is None:
            return
        usdcx = self.config.strategy1.usdcx_symbol
        cc = self.config.strategy1.cc_symbol
        syms = [usdcx, cc] + [s for s in market.pool_token_symbols()
                              if s.upper() not in (usdcx.upper(), cc.upper())]
        symbol = await questionary.select("Token to withdraw:", choices=syms).ask_async()
        if not symbol:
            return

        receiver = (await questionary.text(
            "Receiver address (Canton party id, e.g. Cantex::1220…):").ask_async() or "")
        try:
            receiver = validate_receiver(receiver)
        except WithdrawError as exc:
            console.print(f"[red]{exc}[/red]")
            return

        amount_raw = await questionary.text(
            f"Amount per wallet ({symbol}) — value (e.g. 5) or percent (e.g. 100%):",
            default="100%",
        ).ask_async()
        try:
            amount = AmountSpec.parse(amount_raw or "")
        except AmountError as exc:
            console.print(f"[red]Bad amount: {exc}[/red]")
            return

        # A reserve left behind: every swap pays its network fee in CC, so a
        # wallet drained of CC can no longer trade.
        keep_raw = await questionary.text(
            f"Keep behind in each wallet ({symbol}, 0 = withdraw all):",
            default="1" if symbol.upper() == cc.upper() else "0",
        ).ask_async()
        try:
            keep = Decimal((keep_raw or "0").strip() or "0")
        except InvalidOperation:
            console.print("[red]Bad keep amount.[/red]")
            return
        if keep < 0:
            console.print("[red]Keep must be >= 0.[/red]")
            return

        console.print(
            f"[bold]WITHDRAW[/bold] {amount} {symbol} from "
            f"{len(wallet_names)} wallet(s) → [cyan]{receiver}[/cyan]"
            + (f"  (keeping {keep} {symbol} each)" if keep else "")
        )
        await self._choose_execution_mode()
        if not self.engine.dry_run:
            # Transfers cannot be undone: confirm the destination itself.
            console.print(
                "[bold red]Transfers are irreversible. Funds leave these wallets "
                "for the address above.[/bold red]")
            typed = await questionary.text(
                "Re-type the LAST 6 characters of the receiver address to confirm:"
            ).ask_async()
            if (typed or "").strip() != receiver[-6:]:
                console.print("[yellow]Mismatch — cancelled, nothing sent.[/yellow]")
                return

        outcomes = await withdraw_selected(
            self.manager, wallet_names=wallet_names, symbol=symbol,
            receiver=receiver, amount=amount, keep=keep,
            dry_run=self.engine.dry_run, run_state=self.run_state,
            notifier=self.notifier,
        )
        total = sum((o.amount for o in outcomes if o.ok), Decimal(0))
        for o in outcomes:
            if o.error:
                console.print(f"[{o.wallet}] [red]{o.error}[/red]")
            elif o.skipped:
                console.print(f"[{o.wallet}] [yellow]skip[/yellow] — {o.skipped}")
            else:
                tag = "[green]sent[/green]" if o.sent else "[cyan]dry-run[/cyan]"
                console.print(f"[{o.wallet}] {tag} {o.amount} {o.symbol}")
        ok = sum(1 for o in outcomes if o.ok)
        console.print(f"[bold]{ok}/{len(outcomes)} wallets, {total} {symbol} "
                      f"{'sent' if not self.engine.dry_run else '(dry-run)'}[/bold]")

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

    async def action_add_wallets(self) -> None:
        """Bulk-import wallets from a pasted key dump file into .env + config.toml."""
        from .wallet_import import WalletImportError, append_wallets, parse_dump

        path = (await questionary.path(
            "Path to the wallet dump file "
            "(name / mnemonic / Cantex:: / operator key / trading key per wallet):"
        ).ask_async() or "").strip()
        if not path:
            return
        try:
            wallets = parse_dump(Path(path).read_text())
        except OSError as exc:
            console.print(f"[red]Cannot read {path}: {exc}[/red]")
            return
        except WalletImportError as exc:
            console.print(f"[red]Parse error: {exc}[/red]")
            return

        console.print(
            f"Parsed [bold]{len(wallets)}[/bold] wallet(s): "
            f"{', '.join(w.name for w in wallets)}"
        )
        armed = await questionary.confirm(
            "Append new wallets to .env and config.toml?", default=False
        ).ask_async()
        if not armed:
            console.print("[yellow]Cancelled.[/yellow]")
            return
        try:
            env_add, cfg_add = append_wallets(wallets)
        except WalletImportError as exc:
            console.print(f"[red]{exc}[/red]")
            return
        console.print(
            f"[green]Added[/green] {len(env_add)} to .env, {len(cfg_add)} to "
            f"config.toml ({len(wallets) - len(env_add)} already present)."
        )
        console.print(
            "[dim]Restart the bot to load the new wallets. "
            "Delete the dump file — it holds private keys.[/dim]"
        )

    # -- telegram commands ---------------------------------------------------

    async def _cmd_help(self, _arg: str) -> str:
        return (
            "Cantex bot commands:\n"
            "/stats  — per-wallet target, loss d/w, profit y/w, fee today (all CC)\n"
            "/help   — this message"
        )

    async def _cmd_stats(self, _arg: str) -> str:
        """Per-wallet table (all CC): TARGET (done/target), LOSS d/w, PROFIT y/w
        (rebates - fee - loss), FEE today."""
        from datetime import datetime, timezone

        # Fresh numbers: if the background cache is cold, sweep once inline.
        if all(s.updated == 0 for s in self.service.snaps.values()):
            await self.service.refresh_once()

        default_target = getattr(self.config.strategy1, "daily_swap_target", 0)
        rows: list[tuple[str, str, str, str, str]] = []
        d_done = d_target = 0
        loss_d = loss_w = prof_y = prof_w = fee_sum = Decimal(0)
        for name in self.manager.names:
            s = self.service.snaps.get(name)
            if s is None:
                continue
            v = self.run_state.views.get(name)
            if v is not None and (v.active or v.finished):
                done, target = v.done, v.target or default_target
            else:
                done, target = s.swaps_today, default_target
            d_done += done
            d_target += target
            loss_d += s.loss_today
            loss_w += s.loss_week
            prof_y += s.profit_yesterday
            prof_w += s.profit_week
            fee_sum += s.fee_today
            flag = "" if s.status == "ok" else f" ({s.status})"
            rows.append((
                name[:12],
                f"{done}/{target}",
                f"{_sig(s.loss_today)}/{_sig(s.loss_week)}",
                f"{_sig(s.profit_yesterday)}/{_sig(s.profit_week)}",
                f"{s.fee_today:.3f}{flag}",
            ))

        w0 = max([12] + [len(r[0]) for r in rows])
        w1 = max([6] + [len(r[1]) for r in rows])
        w2 = max([8] + [len(r[2]) for r in rows])
        w3 = max([9] + [len(r[3]) for r in rows])
        header = (f"{'WALLET':<{w0}}  {'TARGET':>{w1}}  {'LOSS d/w':>{w2}}  "
                  f"{'PROFIT y/w':>{w3}}  FEE(CC)")
        lines = [header, "-" * len(header)]
        for name, tgt, loss, prof, fee in rows:
            lines.append(f"{name:<{w0}}  {tgt:>{w1}}  {loss:>{w2}}  {prof:>{w3}}  {fee}")
        total = (
            f"{'TOTAL':<{w0}}  {f'{d_done}/{d_target}':>{w1}}  "
            f"{f'{_sig(loss_d)}/{_sig(loss_w)}':>{w2}}  "
            f"{f'{_sig(prof_y)}/{_sig(prof_w)}':>{w3}}  {fee_sum:.3f}"
        )
        lines += ["-" * len(header), total]
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        title = (f"📊 Cantex stats — {stamp}  ({len(rows)} wallets)  "
                 f"loss/profit/fee in CC")
        return title + "\n\n" + "\n".join(lines)

    # -- main loop -----------------------------------------------------------

    async def menu(self) -> None:
        actions = {
            "Dashboard (live)": self.action_dashboard,
            "Strategy 1": self.action_strategy1,
            "Strategy 2 (auto lowest-fee)": self.action_strategy2,
            "Swap 1x all pairs": self.action_swap_all,
            "Manual swap": self.action_manual_swap,
            "Withdraw (bulk)": self.action_withdraw,
            "Web check (history + rebates)": self.action_web_check,
            "Wallet status": self.action_wallet_status,
            "Add wallets (bulk import)": self.action_add_wallets,
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
