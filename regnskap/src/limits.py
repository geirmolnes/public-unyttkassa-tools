"""Per-user limits on endpoints that cost money or write to the event log.

Kept in memory: the app runs as one process on purpose, so a restart (deploy) resets the counts. The global read
budget is the durable guard: it sums what today's reads cost in the database.
"""

import os
import time
from collections import defaultdict, deque

from fastapi import HTTPException

from src.auth import ADMINS

READ_BUDGET_USD = float(os.environ.get("READ_BUDGET_USD", "20"))


class Window:
    """At most `limit` units per key within any `seconds` long window."""

    def __init__(self, limit: int, seconds: float) -> None:
        self.limit = limit
        self.seconds = seconds
        self._spent: defaultdict[str, deque[tuple[float, int]]] = defaultdict(deque)

    def used(self, key: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        spent = self._spent[key]
        while spent and spent[0][0] <= now - self.seconds:
            spent.popleft()
        return sum(n for _, n in spent)

    def take(self, key: str, n: int = 1, now: float | None = None) -> bool:
        """Spend n units for key; False (nothing spent) when that would go over the limit."""
        now = time.monotonic() if now is None else now
        if self.used(key, now) + n > self.limit:
            return False
        self._spent[key].append((now, n))
        return True

    def clear(self) -> None:
        self._spent.clear()


READ_YEARS = Window(60, 24 * 3600)
SUBSIDIARY_RERUNS = Window(20, 24 * 3600)
CHAT = Window(60, 3600)
CLIENT_LOG = Window(30, 600)
ALL = (READ_YEARS, SUBSIDIARY_RERUNS, CHAT, CLIENT_LOG)


def take(window: Window, email: str, n: int, message: str) -> None:
    """Spend n units of the user's allowance, or raise 429 with `message`. Admins are not limited."""
    if email.lower() in ADMINS:
        return
    if not window.take(email.lower(), n):
        raise HTTPException(429, message)


def reset() -> None:
    for window in ALL:
        window.clear()
