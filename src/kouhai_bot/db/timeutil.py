"""Time helpers — Beijing (UTC+8) ISO timestamps match legacy records."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(BEIJING).isoformat()


def unix_now() -> int:
    return int(time.time())


def beijing_date(ts: int | None = None) -> str:
    if ts is None:
        return datetime.now(BEIJING).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(ts, BEIJING).strftime("%Y-%m-%d")