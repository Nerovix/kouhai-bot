"""Reply budget: passive-first, active-fallback.

Passive replies ride an incoming ``msg_id`` (5 replies within 5 minutes).
When that window/quota is exhausted, fall back to active messages
(1000/group/day, 20/group/minute). The daily active count is persisted in
SQLite so it survives restarts; the per-minute throttle is in-memory.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from ..db.database import Database
from ..db.repos import runtime as runtime_repo
from ..db.timeutil import beijing_date

PASSIVE_WINDOW_SEC = 300.0
PASSIVE_MAX_REPLIES = 5
ACTIVE_DAILY_MAX = 1000
ACTIVE_QPM_MAX = 20


class ReplyBudget:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db
        self._passive: dict[str, deque[float]] = {}
        self._active_qpm: dict[str, deque[float]] = {}
        self._memory_daily: dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()

    def next_passive_seq(self, msg_id: str) -> int | None:
        """Return the next passive msg_seq (1..5), or None if exhausted."""
        if not msg_id:
            return None
        now = time.monotonic()
        window = self._passive.setdefault(msg_id, deque())
        while window and now - window[0] > PASSIVE_WINDOW_SEC:
            window.popleft()
        if len(window) >= PASSIVE_MAX_REPLIES:
            return None
        window.append(now)
        return len(window)

    async def acquire_active(self, group_openid: str) -> bool:
        """Reserve an active-message slot (daily + per-minute quota)."""
        async with self._lock:
            now = time.monotonic()
            qpm = self._active_qpm.setdefault(group_openid, deque())
            while qpm and now - qpm[0] > 60.0:
                qpm.popleft()
            if len(qpm) >= ACTIVE_QPM_MAX:
                return False
            day = beijing_date()
            if self._db is not None:
                count = await runtime_repo.get_active_count(self._db, group_openid, day)
                if count >= ACTIVE_DAILY_MAX:
                    return False
                await runtime_repo.incr_active(self._db, group_openid, day)
            else:
                key = (group_openid, day)
                count = self._memory_daily.get(key, 0)
                if count >= ACTIVE_DAILY_MAX:
                    return False
                self._memory_daily[key] = count + 1
            qpm.append(now)
            return True