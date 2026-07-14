"""Shared live state between a running strategy and the dashboard.

Strategy1 writes per-wallet operational state here (status, current plan, daily
target progress) and the dashboard reads it. When a strategy is active the
dashboard also narrows the balances view to the strategy's selected tokens.
"""
from __future__ import annotations

from dataclasses import dataclass, field


# Per-wallet strategy status values (shown in the dashboard STATUS column).
IDLE = "idle"
RUNNING = "running"
SWAPPING = "swapping"       # a swap is in flight
WAITING = "waiting fee"     # cooldown / waiting between swaps
STOPPED = "stopped"
DONE = "done"
ERROR = "error"


@dataclass
class StratView:
    active: bool = False       # a swap loop is currently running for this wallet
    finished: bool = False     # the run ended (terminal status/plan kept on show)
    status: str = IDLE
    route: str = ""            # trade route, e.g. "buy USDCX→CBTC" (ROUTE column)
    plan: str = ""             # phase text, e.g. "wait fee 0.79" (STATUS column)
    target: int = 0
    done: int = 0


class RunState:
    """Process-wide strategy state, read by the dashboard."""

    def __init__(self) -> None:
        self.views: dict[str, StratView] = {}
        self.strategy_active: bool = False
        # None => show all token balances; a list => only these symbols.
        self.selected_tokens: list[str] | None = None
        # Base token the active/last strategy cycles against (USDCX by default).
        # Loss/profit are measured in this currency then converted to CC.
        self.base_symbol: str = "USDCX"

    def view(self, name: str) -> StratView:
        v = self.views.get(name)
        if v is None:
            v = StratView()
            self.views[name] = v
        return v

    def begin(self, names: list[str], selected_tokens: list[str],
              base_symbol: str = "USDCX") -> None:
        self.strategy_active = True
        self.selected_tokens = list(selected_tokens)
        self.base_symbol = (base_symbol or "USDCX").upper()
        for n in names:
            v = self.view(n)
            v.active = True
            v.finished = False
            v.status = RUNNING
            v.route = ""
            v.plan = ""
            v.done = 0

    def set(self, name: str, *, status: str | None = None, plan: str | None = None,
            route: str | None = None, target: int | None = None,
            done: int | None = None) -> None:
        v = self.view(name)
        if status is not None:
            v.status = status
        if plan is not None:
            v.plan = plan
        if route is not None:
            v.route = route
        if target is not None:
            v.target = target
        if done is not None:
            v.done = done

    def finish(self, name: str, status: str = DONE) -> None:
        v = self.view(name)
        v.status = status
        v.active = False
        v.finished = True

    def end(self) -> None:
        # Keep each view's terminal state (status/plan/done/target) and the
        # selected tokens so the dashboard still shows how the completed run
        # ended and its final balances; only mark the run itself inactive.
        self.strategy_active = False
        for v in self.views.values():
            v.active = False
