"""Curfew (宵禁) — block /submit during configured quiet hours."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import get_config

_TZ = timezone(timedelta(hours=8))


def _normalized_curfew():
    """Return (start, duration) clamped to valid ranges."""
    cfg = get_config()
    start = int(getattr(cfg, "curfew_start_hour", 0) or 0) % 24
    duration = max(0, min(int(getattr(cfg, "curfew_duration_hours", 0) or 0), 24))
    return start, duration


def is_curfew_active() -> bool:
    """True when the current time falls within the configured curfew window."""
    start, duration = _normalized_curfew()
    if duration <= 0:
        return False
    if duration >= 24:
        return True

    now = datetime.now(_TZ)
    current_hour = now.hour
    end = (start + duration) % 24

    if start < end:
        return start <= current_hour < end
    else:
        return current_hour >= start or current_hour < end


def format_curfew_message() -> str:
    """Human-friendly curfew message for blocked /submit replies."""
    start, duration = _normalized_curfew()
    if duration >= 24:
        return "今天是 bot 的休息日哦，请明天再来提交吧～ 🌙"
    if duration <= 0:
        return ""

    end_hour = (start + duration) % 24 or 24
    return (
        f"{start:02d}:00 到 {end_hour:02d}:00 是 bot 的休息时间哦，"
        "请等 bot 起床再试吧～ 🌙"
    )
