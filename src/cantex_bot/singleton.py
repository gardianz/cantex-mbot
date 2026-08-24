"""One bot per state directory.

Two bots trading the same wallets is not a slow-down, it is silent corruption:
each reads the balance, each decides to buy, and both submit. The buy/sell
collision guard (``_confirm_via_history``) reconciles one process against the
exchange — it cannot see a second process at all. The symptom is trades at sizes
the running bot never computed, and a daily target that overshoots.

An advisory ``flock`` on a file beside ``state.db`` makes the second start fail
loudly instead. It is per-machine and per-directory: it cannot catch a bot
started from a different checkout, or on another host.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_LOCK_PATH = "state.db.lock"


class AlreadyRunning(Exception):
    """Another bot holds the lock for this state directory."""


class SingletonLock:
    """Hold for the process lifetime; released when closed or on exit."""

    def __init__(self, path: str | Path = DEFAULT_LOCK_PATH) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> "SingletonLock":
        try:
            import fcntl
        except ImportError:          # non-POSIX: no advisory locks, skip
            return self
        fh = self.path.open("a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip() or "unknown"
            fh.close()
            raise AlreadyRunning(
                f"another cantex-bot is already running here (pid {holder}, "
                f"lock {self.path}). Two bots on the same wallets submit "
                f"conflicting swaps — stop that one first."
            ) from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()      # closing drops the flock
            self._fh = None
