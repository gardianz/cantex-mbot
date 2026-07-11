"""Scalable CLI dashboard: portfolio summary + paged per-wallet rows + log.

Paints entirely from ``PortfolioService`` cache (a background loop fills it), so
render is instant even with hundreds of wallets. Rows show ``loading`` until
their first refresh lands. Scroll with the arrow keys; ``q`` returns to the menu.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from decimal import Decimal

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from .config import AppConfig
from .logging_setup import recent_logs
from .portfolio import PortfolioService, WalletSnap
from .runstate import (
    RunState, IDLE, RUNNING, SWAPPING, WAITING, STOPPED, DONE, ERROR,
)

logger = logging.getLogger(__name__)

_ACCENT = "cyan"
_BORDER = "grey37"
_DIM = "grey42"


def _money(d: Decimal, places: int = 4) -> str:
    try:
        return f"{d:,.{places}f}"
    except (ValueError, TypeError):
        return str(d)


def _num(value: Decimal | int | str, style: str, places: int = 4) -> Text:
    s = value if isinstance(value, str) else (
        f"{value:,}" if isinstance(value, int) else _money(value, places))
    try:
        zero = Decimal(str(value)) == 0
    except Exception:  # noqa: BLE001
        zero = False
    return Text(s, style=_DIM if zero else style)


def _tok_amount(sym: str, v: Decimal) -> str:
    """Balance string: BTC-like tokens keep precision; others show 2 decimals."""
    if "BTC" in sym.upper():
        return f"{v:,.8f}".rstrip("0").rstrip(".") if v else "0"
    return f"{v:,.2f}"


def _fee3(a: Decimal, b: Decimal, c: Decimal) -> Text:
    """Three fee/rebate values as 'a/b/c' (2dp) with dim zeros."""
    def part(x: Decimal):
        return (_money(x, 2), _DIM if x == 0 else "white")
    return Text.assemble(part(a), ("/", _DIM), part(b), ("/", _DIM), part(c))


_STATUS = {
    "ok": ("●", "green3"),
    "error": ("●", "red"),
    "loading": ("○", "grey42"),
}

_RUN_STYLE = {
    RUNNING: "cyan", SWAPPING: "cyan", WAITING: "yellow",
    DONE: "green3", STOPPED: "grey62", ERROR: "red", IDLE: _DIM,
}


class Dashboard:
    def __init__(
        self, service: PortfolioService, config: AppConfig,
        run_state: RunState | None = None,
    ) -> None:
        self.service = service
        self.config = config
        self.run_state = run_state
        self.console = Console()
        self.offset = 0
        self._dirty = asyncio.Event()

    def _default_target(self) -> int:
        s1 = getattr(self.config, "strategy1", None)
        return getattr(s1, "daily_swap_target", 0)

    # -- rendering -----------------------------------------------------------

    def _page_size(self) -> int:
        # header(2) + summary(4) + table head(2) + log(10) + footer(2) ≈ 20
        return max(4, self.console.size.height - 20)

    def render(self) -> Group:
        page = self._page_size()
        names = self.service.manager.names
        n = len(names)
        max_off = max(0, n - page)
        if self.offset > max_off:
            self.offset = max_off
        if self.offset < 0:
            self.offset = 0
        visible = names[self.offset:self.offset + page]
        return Group(
            self._header(),
            Text(""),
            self._summary(),
            Text(""),
            self._wallet_table(visible),
            Text(""),
            self._log_panel(),
            self._footer(n, page),
        )

    def _header(self) -> Table:
        net = self.config.network
        mode, mstyle = ("DRY-RUN", "bold green") if net.dry_run else ("● LIVE", "bold red")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="right")
        grid.add_row(
            Text.assemble(
                ("CANTEX DASHBOARD", f"bold {_ACCENT}"), ("   ", ""),
                (net.base_url, "grey62"),
            ),
            Text.assemble((now + "  ", "grey62"), (mode, mstyle)),
        )
        return grid

    def _summary(self) -> Table:
        t = self.service.totals()
        head = Table.grid(expand=True, padding=(0, 2))
        head.add_column(justify="left")
        head.add_row(Text.assemble(
            ("PORTFOLIO  ", f"bold {_ACCENT}"),
            (f"{t.wallets} wallets", "white"),
            ("   ", ""),
            (f"{t.ok} ok", "green3"), (" / ", _DIM),
            (f"{t.err} err", "red" if t.err else _DIM), (" / ", _DIM),
            (f"{t.loading} loading", "yellow" if t.loading else _DIM),
        ))
        grid = Table.grid(expand=True, padding=(0, 3))
        for _ in range(6):
            grid.add_column(justify="left")
        lo = _money(t.fee_min_today, 2) if t.fee_min_today is not None else "—"
        av = _money(t.fee_avg_today, 2) if t.fee_avg_today is not None else "—"
        grid.add_row(
            Text.assemble(("USDCX ", _DIM), (_money(t.usdcx, 2), "white"),
                          ("  CC ", _DIM), (_money(t.cc, 2), "white")),
            Text.assemble(("fee t/y/w ", _DIM),
                          (_fee3(t.fee_today, t.fee_yesterday, t.fee_this_week))),
            Text.assemble(("feeLo/avg ", _DIM),
                          (lo, "green3"), ("/", _DIM), (av, "yellow")),
            Text.assemble(("rebate y/w/lw ", _DIM),
                          (_fee3(t.reb_yesterday, t.reb_this_week, t.reb_last_week))),
            Text.assemble(("swaps_today ", _DIM), (f"{t.swaps_today:,}", "cyan")),
            Text.assemble(("loss d/w ", _DIM), self._loss_part(t.loss_today),
                          ("/", _DIM), self._loss_part(t.loss_week)),
        )
        return Group(head, grid)

    def _active_view(self, name: str):
        """The strategy view only while a swap loop is live (for the ROUTE cell)."""
        if self.run_state is None:
            return None
        v = self.run_state.views.get(name)
        return v if (v is not None and v.active) else None

    def _strat_view(self, name: str):
        """The strategy view if the wallet has ever run (active OR finished), so a
        completed run's terminal STATUS/SWAP stay on show instead of reverting to
        the rebate note."""
        if self.run_state is None:
            return None
        v = self.run_state.views.get(name)
        return v if (v is not None and (v.active or v.finished)) else None

    def _status_cell(self, name: str, s: WalletSnap) -> Text:
        v = self._strat_view(name)
        if v is not None:
            text = v.plan or v.status
            style = _RUN_STYLE.get(v.status, "cyan")
            return Text(text[:40], style)
        if s.status == "error":
            return Text((s.error or "")[:40], "red")
        note = s.reb_status or "idle"
        return Text(note, "green3" if note.lower().startswith("paid") else _DIM)

    def _swap_cell(self, name: str, s: WalletSnap) -> Text:
        v = self._strat_view(name)
        done, target = (v.done, v.target) if v is not None else (
            s.swaps_today, self._default_target())
        return Text(f"{done}/{target}", "cyan" if done else _DIM)

    @staticmethod
    def _loss_part(v: Decimal) -> tuple[str, str]:
        """(text, style) for one loss value: red if a loss (>0), green if a gain."""
        if v == 0:
            return "0.00", _DIM
        return _money(v, 2), "red" if v > 0 else "green3"

    def _loss_cell(self, day: Decimal, week: Decimal) -> Text:
        """Daily / this-week loss in USDCX as 'd/w'."""
        return Text.assemble(self._loss_part(day), ("/", _DIM), self._loss_part(week))

    def _route_cell(self, name: str) -> Text:
        """The trade route the strategy is on / heading to (own column)."""
        v = self._active_view(name)
        if v is None or not v.route:
            return Text("—", _DIM)
        style = "green3" if v.route.startswith("buy") else "orange3"
        return Text(v.route, style)

    def _cc_symbol(self) -> str:
        s1 = getattr(self.config, "strategy1", None)
        return getattr(s1, "cc_symbol", "CC").upper()

    def _token_header(self) -> str:
        """Header for the TOKEN column: the single selected pair token's symbol,
        else the generic 'TOKEN'."""
        if self.run_state is not None and self.run_state.selected_tokens:
            sel = [t for t in self.run_state.selected_tokens if t.upper() != self._cc_symbol()]
            if len(sel) == 1:
                return sel[0]
        return "TOKEN"

    def _token_cell(self, s: WalletSnap) -> Text:
        if not s.tokens:
            return Text("—", _DIM)
        single = len(s.tokens) == 1
        parts = []
        for sym, amt in s.tokens:
            val = _tok_amount(sym, amt)
            style = "white" if amt else _DIM
            # When the column header is the token symbol, show only the amount.
            parts.append(Text(val, style) if single
                         else Text.assemble((f"{sym} ", _DIM), (val, style)))
        out = parts[0]
        for p in parts[1:]:
            out = Text.assemble(out, ("  ", ""), p)
        return out

    def _wallet_table(self, visible: list[str]) -> Table:
        t = Table(
            box=box.SIMPLE_HEAVY, header_style="bold", border_style=_BORDER,
            expand=True, pad_edge=False, title_style=f"bold {_ACCENT}",
            title="WALLETS", title_justify="left",
        )
        t.add_column("", width=1)                       # status dot
        t.add_column("WALLET", style=_ACCENT, no_wrap=True)
        t.add_column("USDCX", justify="right", no_wrap=True)
        t.add_column("CC", justify="right", no_wrap=True)
        t.add_column(self._token_header(), justify="right", no_wrap=True)
        t.add_column("FEE now", justify="right", no_wrap=True)
        t.add_column("SWAP d/t", justify="right", no_wrap=True)
        t.add_column("LOSS d/w", justify="right", no_wrap=True)
        t.add_column("FEE t/y/w", justify="right", no_wrap=True)
        t.add_column("REB y/w/lw", justify="right", no_wrap=True)
        t.add_column("ROUTE", no_wrap=True, overflow="ellipsis")
        t.add_column("STATUS", ratio=1, no_wrap=True, overflow="ellipsis")
        for name in visible:
            s = self.service.snaps[name]
            dot, dstyle = _STATUS.get(s.status, ("○", _DIM))
            if s.status == "loading":
                t.add_row(Text(dot, dstyle), name, *["[dim]…[/dim]"] * 9, "[dim]loading[/dim]")
                continue
            # FEE now: read live so it tracks the strategy's fresh quotes,
            # not just the slower portfolio sweep.
            live_fee = self.service.store.latest_fee(name)
            fee_now = _money(live_fee, 2) if live_fee is not None else "—"
            t.add_row(
                Text(dot, dstyle), name,
                _num(s.usdcx, "white", 2),
                _num(s.cc, "white", 2),
                self._token_cell(s),
                Text(fee_now, "yellow" if live_fee is not None else _DIM),
                self._swap_cell(name, s),
                self._loss_cell(s.loss_today, s.loss_week),
                _fee3(s.fee_today, s.fee_yesterday, s.fee_this_week),
                _fee3(s.reb_yesterday, s.reb_this_week, s.reb_last_week),
                self._route_cell(name),
                self._status_cell(name, s),
            )
        return t

    def _log_panel(self) -> Table:
        t = Table(
            box=box.SIMPLE, border_style=_BORDER, expand=True, pad_edge=False,
            show_header=False, title="LOG", title_style=f"bold {_ACCENT}",
            title_justify="left",
        )
        t.add_column(no_wrap=True, overflow="ellipsis")
        lines = recent_logs(8)
        if not lines:
            t.add_row(Text("(no log yet)", _DIM))
        for ln in lines:
            style = "red" if " ERROR " in ln or " WARNING " in ln else "grey62"
            t.add_row(Text(ln, style))
        return t

    def _footer(self, n: int, page: int) -> Table:
        start = self.offset + 1 if n else 0
        end = min(self.offset + page, n)
        pages = max(1, (n + page - 1) // page)
        cur = self.offset // page + 1 if page else 1
        t = Table.grid(expand=True, padding=(0, 1))
        t.add_column(justify="left", ratio=1)
        t.add_column(justify="right")
        t.add_row(
            Text.assemble(
                (f"rows {start}-{end}/{n}  ", "grey62"),
                (f"page {cur}/{pages}", "white"),
            ),
            Text("↑↓ scroll · PgUp/Dn · g/G top/end · r refresh · q back", _DIM),
        )
        return t

    # -- key handling --------------------------------------------------------

    def _on_key(self, data: bytes, page: int, n: int) -> bool:
        """Apply a key press. Returns True if the caller should quit."""
        max_off = max(0, n - page)
        if data in (b"q", b"\x1b"):  # q or lone ESC
            return True
        if data in (b"\x1b[A", b"k"):
            self.offset -= 1
        elif data in (b"\x1b[B", b"j"):
            self.offset += 1
        elif data in (b"\x1b[5~", b"\x02"):  # PgUp / Ctrl-B
            self.offset -= page
        elif data in (b"\x1b[6~", b"\x06"):  # PgDn / Ctrl-F
            self.offset += page
        elif data in (b"g", b"\x1b[H"):
            self.offset = 0
        elif data in (b"G", b"\x1b[F"):
            self.offset = max_off
        elif data == b"r":
            asyncio.ensure_future(self.service.refresh_all())
        self.offset = max(0, min(self.offset, max_off))
        self._dirty.set()
        return False

    # -- run -----------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        self.service.start()
        loop = asyncio.get_running_loop()
        quit_flag = {"v": False}
        fd = None
        old_term = None
        istty = sys.stdin.isatty()
        if istty:
            import termios
            import tty
            fd = sys.stdin.fileno()
            old_term = termios.tcgetattr(fd)
            tty.setcbreak(fd)

            def _readable() -> None:
                try:
                    data = os.read(fd, 64)
                except OSError:
                    return
                if self._on_key(data, self._page_size(), len(self.service.manager.names)):
                    quit_flag["v"] = True
                    stop.set()
            loop.add_reader(fd, _readable)
        try:
            with Live(
                self.render(), console=self.console, screen=True,
                auto_refresh=False, transient=True,
            ) as live:
                live.refresh()
                while not stop.is_set() and not quit_flag["v"]:
                    try:
                        await asyncio.wait_for(self._dirty.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    self._dirty.clear()
                    live.update(self.render(), refresh=True)
        finally:
            if istty and fd is not None:
                with contextlib.suppress(Exception):
                    loop.remove_reader(fd)
                import termios
                with contextlib.suppress(Exception):
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
            await self.service.stop()

    async def print_once(self) -> None:
        await self.service.refresh_once()
        self.console.print(self.render())
